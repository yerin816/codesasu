"""레슨 풀이 API 테스트

실행: python manage.py test learning --settings=config.test_settings
(메모리 SQLite에서 돌아가서 Supabase 데이터는 건드리지 않는다)
"""

import json
from datetime import datetime, timezone as dt_timezone
from unittest import mock

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

    def test_week_uses_korean_date(self):
        """한국 시간 월요일 오전 8시 30분(UTC로는 일요일)에 끝낸 레슨도 이번 주로 센다"""
        progress = LessonProgress.objects.create(user=self.user, lesson_id=1)
        LessonProgress.objects.filter(id=progress.id).update(
            completed_at=datetime(2026, 10, 4, 23, 30, tzinfo=dt_timezone.utc)  # 한국 10/5(월) 08:30
        )
        now = datetime(2026, 10, 5, 0, 30, tzinfo=dt_timezone.utc)  # 한국 10/5(월) 09:30
        with mock.patch("django.utils.timezone.now", return_value=now):
            week = self.client.get("/api/v1/today").json()["week"]
        self.assertEqual(week["done_days"], 1)

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


class PageTests(TestCase):
    """3.3 화면과 PWA 파일"""

    fixtures = ["sample_lessons"]

    def test_home_and_lesson_pages_render_and_set_csrf_cookie(self):
        for url in ["/", "/lessons/1/"]:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, url)
            self.assertIn("csrftoken", res.cookies, url)
            self.assertContains(res, 'rel="manifest"')

    def test_manifest_is_valid_and_icons_exist(self):
        from django.contrib.staticfiles import finders

        res = self.client.get("/manifest.webmanifest")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/manifest+json")
        data = json.loads(res.content)
        self.assertEqual(data["display"], "standalone")
        sizes = {icon["sizes"] for icon in data["icons"]}
        self.assertTrue({"192x192", "512x512"} <= sizes)
        for icon in data["icons"]:
            path = icon["src"].replace("/static/", "", 1)
            self.assertIsNotNone(finders.find(path), icon["src"])

    def test_service_worker_served_from_root_without_cache(self):
        res = self.client.get("/sw.js")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/javascript")
        self.assertIn("no-cache", res["Cache-Control"])


class AuthPageTests(TestCase):
    """로그인·가입 화면이 코드사수 틀과 한국어로 나온다 (3.4)"""

    def test_signup_and_login_use_codesasu_layout_in_korean(self):
        for url, title in [("/accounts/signup/", "회원가입"), ("/accounts/login/", "로그인")]:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, url)
            self.assertContains(res, 'class="topbar"')   # 코드사수 머리말
            self.assertContains(res, 'class="auth"')
            self.assertContains(res, title)
            self.assertNotContains(res, "Menu:")          # allauth 기본 영어 메뉴는 없음


def fake_embed(texts):
    """테스트용 가짜 임베딩: OpenAI를 부르지 않고 1536차원 숫자 목록을 만든다"""
    return [[(i + 1) / 1000] * 1536 for i, _ in enumerate(texts)], 10 * len(texts)


class ChunkSplitTests(TestCase):
    def test_long_paragraph_is_split_with_one_sentence_overlap(self):
        from .chunks import split_into_chunks

        sentences = [f"{i}번째 문장은 코드를 한 줄씩 읽는 연습을 설명하는 꽤 긴 문장이에요." for i in range(12)]
        chunks = split_into_chunks(" ".join(sentences))
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 300)
        # 모든 문장이 어딘가에 들어 있고, 다음 조각은 앞 조각의 마지막 문장으로 시작한다
        for s in sentences:
            self.assertTrue(any(s in c for c in chunks), s)
        for prev, nxt in zip(chunks, chunks[1:]):
            last_sentence = prev.split(". ")[-1]
            self.assertTrue(nxt.startswith(last_sentence.rstrip(".")), (prev[-40:], nxt[:40]))

    def test_paragraph_break_starts_new_chunk(self):
        from .chunks import split_into_chunks

        self.assertEqual(split_into_chunks("첫 문단이에요.\n\n둘째 문단이에요."), ["첫 문단이에요.", "둘째 문단이에요."])


@mock.patch("learning.management.commands.ingest_chunks.embed_texts", side_effect=fake_embed)
class IngestChunksTests(TestCase):
    fixtures = ["sample_lessons"]

    def run_command(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("ingest_chunks", *args, stdout=out)
        return out.getvalue()

    def test_creates_chunks_then_skips_unchanged_lessons(self, embed):
        from .models import LessonChunk

        self.run_command()
        self.assertEqual(embed.call_count, 2)                      # 레슨 2개
        self.assertEqual(LessonChunk.objects.count(), 8)          # 레슨마다 문단 4개
        self.assertEqual(len(LessonChunk.objects.first().embedding), 1536)

        first_ids = list(LessonChunk.objects.filter(lesson_id=1).values_list("id", flat=True))
        out = self.run_command()                                   # 바뀐 게 없으면 API를 부르지 않는다
        self.assertEqual(embed.call_count, 2)
        self.assertIn("2개는 바뀐 게 없어", out)

        Lesson.objects.filter(id=2).update(body_md="바뀐 본문이에요.")
        self.run_command()                                         # 바뀐 레슨 2만 다시 만든다
        self.assertEqual(embed.call_count, 3)
        self.assertEqual(list(LessonChunk.objects.filter(lesson_id=1).values_list("id", flat=True)), first_ids)
        self.assertEqual(LessonChunk.objects.filter(lesson_id=2).count(), 1)

    def test_all_option_rebuilds_everything(self, embed):
        self.run_command()
        self.run_command("--all")
        self.assertEqual(embed.call_count, 4)

    def test_dry_run_calls_nothing_and_saves_nothing(self, embed):
        from .models import LessonChunk

        out = self.run_command("--dry-run")
        self.assertEqual(embed.call_count, 0)
        self.assertEqual(LessonChunk.objects.count(), 0)
        self.assertIn("미리보기 완료", out)

    def test_answers_never_go_into_chunks(self, embed):
        """해설에는 정답이 들어 있어, 힌트 근거(청크)에 섞이면 안 된다"""
        from .models import LessonChunk, Question

        self.run_command()
        all_text = " ".join(LessonChunk.objects.values_list("content", flat=True))
        for q in Question.objects.all():
            self.assertNotIn(q.explanation[:20], all_text)


class EmbeddingKeyTests(TestCase):
    fixtures = ["sample_lessons"]

    def test_missing_key_stops_with_clear_message_and_saves_nothing(self):
        from django.core.management import CommandError, call_command

        from .models import LessonChunk

        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            with self.assertRaisesMessage(CommandError, "OPENAI_API_KEY"):
                call_command("ingest_chunks")
        self.assertEqual(LessonChunk.objects.count(), 0)


class EmbeddingClientTests(TestCase):
    """OpenAI 응답 처리 (실제 호출 없이 응답을 흉내 낸다)"""

    def fake_response(self, status, body):
        res = mock.Mock(status_code=status)
        res.json.return_value = body
        res.text = json.dumps(body)
        return res

    @mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-not-real"})
    def test_keeps_input_order_and_counts_tokens(self):
        from .embeddings import embed_texts

        body = {"data": [{"index": 1, "embedding": [0.2] * 1536}, {"index": 0, "embedding": [0.1] * 1536}],
                "usage": {"total_tokens": 7}}
        with mock.patch("learning.embeddings.requests.post", return_value=self.fake_response(200, body)) as post:
            vectors, tokens = embed_texts(["가", "나"])
        self.assertEqual([v[0] for v in vectors], [0.1, 0.2])   # index 순서대로 다시 정렬
        self.assertEqual(tokens, 7)
        self.assertEqual(post.call_args.kwargs["json"]["model"], "text-embedding-3-small")

    @mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-not-real"})
    def test_wrong_dimension_and_api_error_are_reported_without_key(self):
        from .embeddings import EmbeddingError, embed_texts

        bad_dim = {"data": [{"index": 0, "embedding": [0.1] * 10}], "usage": {}}
        with mock.patch("learning.embeddings.requests.post", return_value=self.fake_response(200, bad_dim)):
            with self.assertRaisesMessage(EmbeddingError, "차원"):
                embed_texts(["가"])

        err = {"error": {"message": "Incorrect API key provided: sk-proj-*****************Ec0A. You can find your API key"}}
        with mock.patch("learning.embeddings.requests.post", return_value=self.fake_response(401, err)):
            with self.assertRaises(EmbeddingError) as ctx:
                embed_texts(["가"])
        self.assertIn("401", str(ctx.exception))
        self.assertNotIn("sk-test-not-real", str(ctx.exception))
        self.assertNotIn("Ec0A", str(ctx.exception))   # OpenAI가 보낸 가린 키 끝자리도 지운다
