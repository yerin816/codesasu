"""레슨 풀이 API 테스트

실행: python manage.py test learning --settings=config.test_settings
(메모리 SQLite에서 돌아가서 Supabase 데이터는 건드리지 않는다)
"""

import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from .models import AnswerAttempt, ExplanationReveal, Lesson, LessonProgress

# 샘플 레슨 정답: 문제1=2, 문제2=1 (레슨1) / 문제3=1, 문제4=2 (레슨2)
ANSWERS = {1: 2, 2: 1, 3: 1, 4: 2}
LESSON_QUESTIONS = {1: [1, 2], 2: [3, 4]}


class ApiTestCase(TestCase):
    fixtures = ["sample_lessons"]

    def post(self, url, data=None, client=None):
        client = client or self.client
        return client.post(url, json.dumps(data or {}), content_type="application/json")

    def start(self, lesson_id, client=None):
        res = self.post(f"/api/v1/lessons/{lesson_id}/progress", client=client)
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()["progress_id"]

    def answer(self, progress_id, question_id, selected, client=None):
        return self.post(
            f"/api/v1/progress/{progress_id}/answers",
            {"question_id": question_id, "selected": selected},
            client=client,
        )

    def solve_all(self, lesson_id, client=None):
        """레슨의 모든 문제를 한 번에 맞히고 완료한다."""
        pid = self.start(lesson_id, client)
        for qid in LESSON_QUESTIONS[lesson_id]:
            self.answer(pid, qid, ANSWERS[qid], client)
        res = self.post(f"/api/v1/progress/{pid}/complete", client=client)
        self.assertEqual(res.status_code, 200, res.content)
        return pid, res.json()

    def make_user(self, email="user@example.com"):
        return get_user_model().objects.create_user(
            username=email.split("@")[0], email=email, password="test-pass-1234!"
        )


class GuestTests(ApiTestCase):
    def test_today_shows_first_lesson_for_guest(self):
        res = self.client.get("/api/v1/today")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["lesson"]["id"], 1)
        self.assertTrue(body["is_guest"])
        self.assertIsNone(body["week"])
        # 한글이 \uXXXX로 바뀌지 않고 그대로 나간다
        self.assertIn("반복문은", res.content.decode())

    def test_lesson_detail_never_sends_answer_or_explanation(self):
        res = self.client.get("/api/v1/lessons/1")
        self.assertEqual(res.status_code, 200)
        questions = res.json()["questions"]
        self.assertEqual([q["id"] for q in questions], [1, 2])
        for q in questions:
            self.assertNotIn("answer", q)
            self.assertNotIn("explanation", q)
        # 키 이름을 바꿔 새는 경우까지 막기 위해 원문 해설 문장도 응답에 없는지 본다
        self.assertNotIn("박서연 한 명이라", res.content.decode())

    def test_guest_cannot_open_second_lesson(self):
        res = self.client.get("/api/v1/lessons/2")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "NOT_AUTHENTICATED")
        res = self.post("/api/v1/lessons/2/progress")
        self.assertEqual(res.status_code, 401)

    def test_guest_full_flow_with_wrong_streak(self):
        pid = self.start(1)

        first = self.answer(pid, 1, 1).json()  # 오답
        self.assertFalse(first["is_correct"])
        self.assertIsNone(first["explanation"])  # 틀리면 해설(=정답)을 주지 않는다
        self.assertEqual(first["wrong_streak"], 1)
        self.assertFalse(first["suggest_hint"])

        second = self.answer(pid, 1, 3).json()  # 또 오답 → 힌트 제안 (FR-09)
        self.assertEqual(second["wrong_streak"], 2)
        self.assertTrue(second["suggest_hint"])

        third = self.answer(pid, 1, 2).json()  # 정답
        self.assertTrue(third["is_correct"])
        self.assertEqual(third["wrong_streak"], 0)
        self.assertTrue(third["explanation"])

        self.answer(pid, 2, 1)  # 문제2는 한 번에 정답
        done = self.post(f"/api/v1/progress/{pid}/complete").json()
        # 처음 낸 답 기준: 문제1 오답, 문제2 정답 → 1개
        self.assertEqual(done["correct_count"], 1)
        self.assertEqual(done["question_count"], 2)
        self.assertGreaterEqual(done["duration_sec"], 0)
        self.assertIsNone(done["week"])

        progress = LessonProgress.objects.get(id=pid)
        self.assertIsNone(progress.user)
        self.assertTrue(progress.guest_key)
        self.assertIsNotNone(progress.completed_at)

    def test_other_guest_cannot_use_my_progress(self):
        pid = self.start(1)
        stranger = Client()
        res = self.answer(pid, 1, 2, client=stranger)
        self.assertEqual(res.status_code, 404)


class ValidationTests(ApiTestCase):
    def setUp(self):
        self.pid = self.start(1)

    def test_bad_choice_numbers(self):
        for bad in [0, 5, -1, True, "2", None, 2.0]:
            res = self.answer(self.pid, 1, bad)
            self.assertEqual(res.status_code, 400, f"selected={bad!r}")
            self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertEqual(AnswerAttempt.objects.count(), 0)

    def test_question_from_other_lesson(self):
        res = self.answer(self.pid, 3, 1)  # 문제3은 레슨2 소속
        self.assertEqual(res.status_code, 400)

    def test_broken_json(self):
        res = self.client.post(
            f"/api/v1/progress/{self.pid}/answers", "{not json", content_type="application/json"
        )
        self.assertEqual(res.status_code, 400)

    def test_complete_before_answering_everything(self):
        self.answer(self.pid, 1, 2)
        res = self.post(f"/api/v1/progress/{self.pid}/complete")
        self.assertEqual(res.status_code, 400)
        self.assertIsNone(LessonProgress.objects.get(id=self.pid).completed_at)

    def test_no_answers_after_complete_and_complete_is_idempotent(self):
        self.answer(self.pid, 1, 2)
        self.answer(self.pid, 2, 1)
        first = self.post(f"/api/v1/progress/{self.pid}/complete").json()
        res = self.answer(self.pid, 1, 1)
        self.assertEqual(res.status_code, 400)
        again = self.post(f"/api/v1/progress/{self.pid}/complete").json()
        self.assertEqual(first, again)

    def test_unknown_progress_and_wrong_method(self):
        self.assertEqual(self.answer(9999, 1, 2).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/progress/{self.pid}/answers").status_code, 405)

    def test_unpublished_lesson_is_hidden(self):
        Lesson.objects.filter(id=1).update(is_published=False)
        self.assertEqual(self.client.get("/api/v1/lessons/1").status_code, 404)


class ExplanationTests(ApiTestCase):
    """10/7 결정 A: 틀리면 해설을 숨기고, '다시 풀기' 또는 '해설 보기'를 고른다"""

    def setUp(self):
        self.pid = self.start(1)

    def reveal(self, question_id, client=None):
        return self.post(
            f"/api/v1/progress/{self.pid}/explanation", {"question_id": question_id}, client=client
        )

    def test_cannot_reveal_before_answering(self):
        res = self.reveal(1)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(ExplanationReveal.objects.count(), 0)

    def test_reveal_after_wrong_then_no_more_answers(self):
        self.answer(self.pid, 1, 1)  # 오답
        res = self.reveal(1)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["answer"], 2)
        self.assertTrue(body["explanation"])

        # 해설을 본 뒤에는 같은 문제를 다시 낼 수 없다
        res = self.answer(self.pid, 1, 2)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(AnswerAttempt.objects.filter(progress_id=self.pid).count(), 1)

        # 두 번 눌러도 기록은 하나
        self.assertEqual(self.reveal(1).status_code, 200)
        self.assertEqual(ExplanationReveal.objects.count(), 1)

        # 해설을 본 문제가 있어도 레슨은 끝낼 수 있고, 맞힌 수에는 들어가지 않는다
        self.answer(self.pid, 2, 1)
        done = self.post(f"/api/v1/progress/{self.pid}/complete").json()
        self.assertEqual(done["correct_count"], 1)

    def test_no_answers_after_correct(self):
        self.assertTrue(self.answer(self.pid, 1, 2).json()["is_correct"])
        res = self.answer(self.pid, 1, 1)
        self.assertEqual(res.status_code, 400)

    def test_other_person_cannot_reveal(self):
        self.answer(self.pid, 1, 1)
        res = self.reveal(1, client=Client())
        self.assertEqual(res.status_code, 404)
        self.assertEqual(ExplanationReveal.objects.count(), 0)


class LoggedInTests(ApiTestCase):
    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)

    def test_today_moves_to_next_lesson_and_counts_week(self):
        body = self.client.get("/api/v1/today").json()
        self.assertEqual(body["lesson"]["id"], 1)
        self.assertEqual(body["week"], {"done_days": 0, "goal_days": 5})

        _, done = self.solve_all(1)
        self.assertEqual(done["correct_count"], 2)
        self.assertEqual(done["week"], {"done_days": 1, "goal_days": 5})

        body = self.client.get("/api/v1/today").json()
        self.assertEqual(body["lesson"]["id"], 2)

        _, done = self.solve_all(2)
        self.assertEqual(done["week"]["done_days"], 1)  # 같은 날 2개 끝내도 1일

        self.assertIsNone(self.client.get("/api/v1/today").json()["lesson"])

    def test_cannot_touch_other_users_progress(self):
        pid = self.start(1)
        other = Client()
        other.force_login(self.make_user("other@example.com"))
        self.assertEqual(self.answer(pid, 1, 2, client=other).status_code, 404)
        self.assertEqual(self.post(f"/api/v1/progress/{pid}/complete", client=other).status_code, 404)


class GuestToAccountTests(ApiTestCase):
    def test_guest_record_is_attached_after_login(self):
        """게스트로 첫 레슨을 푼 뒤 이메일로 로그인하면 그 기록이 계정에 붙는다 (FR-01)"""
        pid, _ = self.solve_all(1)
        user = self.make_user()

        # allauth 로그인 화면으로 실제 로그인 (세션 키가 바뀌는 상황까지 확인)
        res = self.client.post(
            "/accounts/login/", {"login": "user@example.com", "password": "test-pass-1234!"}
        )
        self.assertEqual(res.status_code, 302, res.content[:300])

        progress = LessonProgress.objects.get(id=pid)
        self.assertEqual(progress.user, user)
        self.assertEqual(AnswerAttempt.objects.filter(progress=progress, user=user).count(), 2)

        # 로그인 뒤 오늘의 레슨은 2번으로 넘어간다
        self.assertEqual(self.client.get("/api/v1/today").json()["lesson"]["id"], 2)


class CsrfTests(ApiTestCase):
    def test_post_needs_csrf_token(self):
        browser = Client(enforce_csrf_checks=True)
        res = self.post("/api/v1/lessons/1/progress", client=browser)
        self.assertEqual(res.status_code, 403)

        browser.get("/api/v1/today")  # 여기서 csrftoken 쿠키를 받는다
        token = browser.cookies["csrftoken"].value
        res = browser.post(
            "/api/v1/lessons/1/progress", "{}", content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(res.status_code, 201)
