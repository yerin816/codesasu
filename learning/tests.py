"""레슨 풀이 API 테스트

실행: python manage.py test learning --settings=config.test_settings
(메모리 SQLite에서 돌아가서 Supabase 데이터는 건드리지 않는다)
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase

from django.utils import timezone

from .models import AnswerAttempt, ExplanationReveal, Lesson, LessonProgress, Profile

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
        self.assertNotIn("세 번째 사람(영업, 연차 5) 한 명이라", res.content.decode())

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


class LeakCheckTests(TestCase):
    """정답 노출 검사 (NFR-04). 문제1 정답=2번 '1', 문제2 정답=1번 긴 문장"""

    fixtures = ["sample_lessons"]

    def setUp(self):
        from .models import Question

        self.q_short = Question.objects.get(id=1)   # 선택지 "0","1","2","3", 정답 2번("1")
        self.q_long = Question.objects.get(id=2)    # 정답 1번: "폴더의 엑셀 파일 목록을 만든다 → ..."

    def leaks(self, msg, q):
        from .hints import find_leaks

        return find_leaks(msg, q)

    def test_catches_answer_number_value_and_phrases(self):
        self.assertTrue(self.leaks("정답은 2번이에요.", self.q_short))
        self.assertTrue(self.leaks("②를 다시 보세요.", self.q_short))
        self.assertTrue(self.leaks("결과는 1이에요.", self.q_short))
        self.assertTrue(self.leaks("1이 출력돼요.", self.q_short))
        self.assertTrue(self.leaks("파일 목록을 만든다 → 하나씩 읽어 모은다 순서예요.", self.q_long))
        self.assertTrue(self.leaks("count = 0\nfor p in staff:\n    count += 1", self.q_short))

    def test_allows_normal_hints(self):
        self.assertEqual(self.leaks("if가 있는 줄의 두 조건을 각각 확인해 볼까요?", self.q_short), [])
        self.assertEqual(self.leaks("2번째 줄부터 한 행씩 따라가 보세요.", self.q_short), [])
        self.assertEqual(self.leaks("`>=`는 같은 값도 포함한다는 점을 떠올려 보세요.", self.q_short), [])
        self.assertEqual(self.leaks("for 아래 들여쓴 줄만 반복된다는 점을 기억해 보세요.", self.q_long), [])


def fake_chat(messages_by_call):
    """chat_json 대신 쓰는 가짜: 호출될 때마다 준비된 답을 차례로 돌려준다"""
    calls = iter(messages_by_call)

    def _chat(system, user, name, schema):
        _chat.prompts.append(system + "\n" + user)
        return {"message": next(calls), "used_excerpts": [1]}, {"tokens_in": 100, "tokens_out": 30, "reasoning_tokens": 10}

    _chat.prompts = []
    return _chat


@mock.patch("learning.hints.embed_texts", side_effect=fake_embed)
class HintApiTests(ApiTestCase):
    def setUp(self):
        from io import StringIO

        from django.core.management import call_command

        with mock.patch("learning.management.commands.ingest_chunks.embed_texts", side_effect=fake_embed):
            call_command("ingest_chunks", stdout=StringIO())
        from . import hints
        hints._question_vectors.clear()
        self.pid = self.start(1)

    def hint(self, level, question_id=1, client=None):
        return self.post(f"/api/v1/questions/{question_id}/hints", {"progress_id": self.pid, "level": level}, client=client)

    def test_three_levels_in_order_with_sources_and_remaining(self, _embed):
        chat = fake_chat(["어디를 봐야 할까요?", "if 줄의 조건을 보세요.", "and는 둘 다 참이어야 해요."])
        with mock.patch("learning.hints.chat_json", side_effect=chat):
            r1 = self.hint(1)
            self.assertEqual(r1.status_code, 201, r1.content)
            body = r1.json()
            self.assertEqual(body["level"], 1)
            self.assertEqual(body["remaining_today"], 19)
            self.assertTrue(body["sources"] and "title" in body["sources"][0])
            self.assertEqual(self.hint(2).status_code, 201)
            self.assertEqual(self.hint(3).json()["remaining_today"], 17)
        # 3단계 요청에는 앞 단계 힌트가 같이 들어가 반복을 피한다
        self.assertIn("어디를 봐야 할까요?", chat.prompts[2])

    def test_cannot_skip_level_and_repeat_is_free(self, _embed):
        chat = fake_chat(["어디를 봐야 할까요?"])
        with mock.patch("learning.hints.chat_json", side_effect=chat):
            r = self.hint(2)
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["error"]["code"], "HINT_LEVEL_SKIPPED")
            first = self.hint(1).json()
            again = self.hint(1)                       # 같은 단계 다시: 모델을 부르지 않고 그대로
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["hint_id"], first["hint_id"])
        self.assertEqual(len(chat.prompts), 1)

    def test_leak_is_regenerated_then_blocked_with_fixed_message(self, _embed):
        from .hints import FALLBACK
        from .models import HintRequest

        with mock.patch("learning.hints.chat_json", side_effect=fake_chat(["정답은 2번이에요.", "괜찮은 힌트예요."])):
            body = self.hint(1).json()
        self.assertEqual(body["message"], "괜찮은 힌트예요.")      # 한 번 다시 만들어 통과
        self.assertFalse(HintRequest.objects.get().blocked)

        with mock.patch("learning.hints.chat_json", side_effect=fake_chat(["결과는 1이에요.", "1이 출력돼요."])):
            body = self.hint(2).json()
        self.assertEqual(body["message"], FALLBACK[2])             # 두 번 다 새면 고정 문구
        self.assertTrue(HintRequest.objects.get(level=2).blocked)

    def test_daily_limit(self, _embed):
        from .models import HintRequest

        for _ in range(20):
            p = self.start(1)
            HintRequest.objects.create(progress_id=p, question_id=1, level=1, response="x", model="m")
        r = self.hint(1)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["error"]["code"], "HINT_LIMIT")

    def test_ai_failure_returns_503_and_uses_no_quota(self, _embed):
        from .llm import LLMError
        from .models import HintRequest

        with mock.patch("learning.hints.chat_json", side_effect=LLMError("down")):
            with self.assertLogs("learning.views", level="WARNING") as logs:
                r = self.hint(1)
        self.assertIn("down", logs.output[0])   # 실패 원인이 서버 로그에 남는다
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"]["code"], "AI_UNAVAILABLE")
        self.assertEqual(HintRequest.objects.count(), 0)

    def test_others_progress_and_wrong_question(self, _embed):
        with mock.patch("learning.hints.chat_json", side_effect=fake_chat(["힌트"])):
            self.assertEqual(self.hint(1, client=Client()).status_code, 404)   # 남의 풀이
            self.assertEqual(self.hint(1, question_id=3).status_code, 404)     # 다른 레슨의 문제

    def test_no_hint_after_correct_answer(self, _embed):
        self.answer(self.pid, 1, 2)
        with mock.patch("learning.hints.chat_json", side_effect=fake_chat(["힌트"])):
            self.assertEqual(self.hint(1).status_code, 400)

    def test_prompt_has_no_user_info(self, _embed):
        """OpenAI로 보내는 내용에 사용자 이메일이 들어가지 않는다 (NFR-05)"""
        user = self.make_user("secret-person@example.com")
        self.client.force_login(user)
        self.pid = self.start(1)
        chat = fake_chat(["어디를 봐야 할까요?"])
        with mock.patch("learning.hints.chat_json", side_effect=chat):
            self.assertEqual(self.hint(1).status_code, 201)
        self.assertNotIn("secret-person", chat.prompts[0])


@mock.patch("learning.hints.embed_texts", side_effect=fake_embed)
class HintCheckCommandTests(TestCase):
    fixtures = ["sample_lessons"]

    def test_runs_all_levels_without_saving(self, _embed):
        from io import StringIO

        from django.core.management import call_command

        from .models import HintRequest

        with mock.patch("learning.management.commands.ingest_chunks.embed_texts", side_effect=fake_embed):
            call_command("ingest_chunks", stdout=StringIO())
        out = StringIO()
        with mock.patch("learning.hints.chat_json", side_effect=fake_chat(["어디를 볼까요?"] * 12)):
            call_command("hint_check", stdout=out)
        text = out.getvalue()
        self.assertIn("힌트 12개", text)
        self.assertIn("고정 문구로 막음 0개", text)
        self.assertEqual(HintRequest.objects.count(), 0)


class DeploySettingsTests(SimpleTestCase):
    """배포(DEBUG=False)일 때 보안 설정이 켜지는지, 실제 settings.py를 새 파이썬으로 따로 읽어 확인한다"""

    KEYS = ["DEBUG", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "SECURE_SSL_REDIRECT", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE",
            "ALLAUTH_TRUSTED_PROXY_COUNT", "ADMIN_PATH"]

    def read_settings(self, **env):
        code = f"import json, config.settings as s; print(json.dumps({{k: getattr(s, k, None) for k in {self.KEYS}}}))"
        # Windows는 SYSTEMROOT 등이 없으면 파이썬이 안 뜨므로 지금 환경을 그대로 넘기고 필요한 값만 덮어쓴다.
        # .env는 이미 있는 환경변수를 덮어쓰지 않으므로, 여기서 넘긴 값이 그대로 쓰인다.
        base = {**os.environ, "DJANGO_SECRET_KEY": "x", "DATABASE_URL": "postgres://u:p@localhost:5432/db",
                "GOOGLE_CLIENT_ID": "x", "GOOGLE_CLIENT_SECRET": "x", "ALLOWED_HOSTS": ""}
        out = subprocess.run([sys.executable, "-c", code], env={**base, **env}, cwd=str(settings.BASE_DIR),
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_production_turns_on_https_settings(self):
        s = self.read_settings(DEBUG="False", ALLOWED_HOSTS="codesasu-abc.a.run.app")
        self.assertFalse(s["DEBUG"])
        self.assertEqual(s["ALLOWED_HOSTS"], ["codesasu-abc.a.run.app"])
        self.assertEqual(s["CSRF_TRUSTED_ORIGINS"], ["https://codesasu-abc.a.run.app"])
        self.assertTrue(s["SECURE_SSL_REDIRECT"] and s["SESSION_COOKIE_SECURE"] and s["CSRF_COOKIE_SECURE"])
        self.assertEqual(s["ALLAUTH_TRUSTED_PROXY_COUNT"], 1)  # Cloud Run 앞단 1단계만 믿는다

    def test_admin_path_comes_from_env(self):
        self.assertEqual(self.read_settings(DEBUG="True")["ADMIN_PATH"], "admin/")
        self.assertEqual(self.read_settings(DEBUG="False", ADMIN_PATH="/manage-x7/")["ADMIN_PATH"], "manage-x7/")

    def test_local_development_keeps_them_off(self):
        s = self.read_settings(DEBUG="True")
        self.assertTrue(s["DEBUG"])
        self.assertFalse(s["SECURE_SSL_REDIRECT"])
        self.assertIsNone(s["ALLAUTH_TRUSTED_PROXY_COUNT"])


class LessonCodeWidthTests(SimpleTestCase):
    """문제 코드가 폰 화면에서 잘리지 않는 폭인지 (10/8 아이폰에서 `>= 5`가 잘려 보이지 않았던 문제)"""

    def test_sample_lesson_code_fits_phone(self):
        from pathlib import Path

        from .models import long_code_lines

        data = json.loads((Path(__file__).parent / "fixtures" / "sample_lessons.json").read_text(encoding="utf-8"))
        for obj in data:
            if obj["model"] == "learning.question":
                self.assertEqual(long_code_lines(obj["fields"]["code"]), [], f"문제 {obj['pk']}")

    def test_admin_rejects_long_line(self):
        from django.core.exceptions import ValidationError

        from .models import Question

        long_line = '    {"이름": "김하늘", "부서": "영업", "연차": 3},'  # 한글 포함 50칸
        q = Question(code=f"staff = [\n{long_line}\n]")
        with self.assertRaises(ValidationError) as ctx:
            q.clean()
        self.assertIn("2번째 줄", str(ctx.exception))
        Question(code="count = 0\nprint(count)").clean()  # 짧은 코드는 통과


class AdminSecurityTests(TestCase):
    """관리자 화면 보호 (6.1): 일반 로그인 화면을 거치고, 비밀번호를 여러 번 틀리면 막힌다"""

    def make_google_user(self, email="owner@example.com"):
        from allauth.socialaccount.models import SocialAccount

        user = get_user_model().objects.create_user(username="owner", email=email)
        SocialAccount.objects.create(user=user, provider="google", uid="g-123")
        return user

    def test_admin_login_goes_through_allauth_login(self):
        res = self.client.get("/admin/")
        self.assertEqual(res.status_code, 302)
        res = self.client.get(res["Location"])
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res["Location"].startswith("/accounts/login/"))

    def test_non_staff_gets_403_and_staff_gets_in(self):
        user = get_user_model().objects.create_user(username="u", email="u@example.com", password="pw-12345678")
        self.client.force_login(user)
        self.assertEqual(self.client.get("/admin/login/").status_code, 403)
        user.is_staff = True
        user.save()
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_login_blocked_after_five_wrong_passwords(self):
        from allauth.account.models import EmailAddress

        user = get_user_model().objects.create_user(username="v", email="v@example.com", password="right-pass-123")
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
        for _ in range(5):
            res = self.client.post("/accounts/login/", {"login": "v@example.com", "password": "wrong-pass"})
            self.assertEqual(res.status_code, 200)
        # 6번째에는 맞는 비밀번호여도 막힌다
        res = self.client.post("/accounts/login/", {"login": "v@example.com", "password": "right-pass-123"})
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_works_after_fewer_failures(self):
        # 대조군: 4번 틀린 뒤에는 맞는 비밀번호로 로그인된다 (위 테스트가 다른 이유로 막힌 게 아님을 확인)
        from allauth.account.models import EmailAddress

        user = get_user_model().objects.create_user(username="w", email="w@example.com", password="right-pass-123")
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
        for _ in range(4):
            self.client.post("/accounts/login/", {"login": "w@example.com", "password": "wrong-pass"})
        self.client.post("/accounts/login/", {"login": "w@example.com", "password": "right-pass-123"})
        self.assertEqual(self.client.session.get("_auth_user_id"), str(user.pk))

    def test_grant_admin_only_for_google_linked_user(self):
        from io import StringIO

        from django.core.management import CommandError, call_command

        get_user_model().objects.create_user(username="p", email="p@example.com", password="x-12345678")
        with self.assertRaises(CommandError):
            call_command("grant_admin", "p@example.com", stdout=StringIO())
        user = self.make_google_user()
        call_command("grant_admin", "OWNER@example.com", stdout=StringIO())
        user.refresh_from_db()
        self.assertTrue(user.is_staff and user.is_superuser)

    def test_grant_admin_from_env_never_fails_startup(self):
        from io import StringIO

        from django.core.management import call_command

        user = self.make_google_user()
        out = StringIO()
        with mock.patch.dict("os.environ", {"ADMIN_EMAILS": "nobody@example.com, owner@example.com"}):
            call_command("grant_admin", "--from-env", stdout=out)  # 없는 이메일이 섞여도 예외 없이 진행
        user.refresh_from_db()
        self.assertTrue(user.is_staff)
        self.assertIn("건너뜀", out.getvalue())
        with mock.patch.dict("os.environ", {"ADMIN_EMAILS": ""}):
            call_command("grant_admin", "--from-env", stdout=StringIO())  # 비어 있어도 그대로 통과


class EnsureRlsTests(TestCase):
    fixtures = ["sample_lessons"]

    def test_skips_quietly_on_sqlite_and_turns_rls_on_for_postgres(self):
        from io import StringIO

        from django.core.management import call_command
        from django.db import connection

        out = StringIO()
        call_command("ensure_rls", stdout=out)
        if connection.vendor != "postgresql":
            self.assertIn("건너뜀", out.getvalue())
            return
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND NOT rowsecurity")
            self.assertEqual(cursor.fetchone()[0], 0)
        out = StringIO()
        call_command("ensure_rls", stdout=out)  # 두 번째는 켤 표가 없다
        self.assertIn("새로 켬 0개", out.getvalue())
        # RLS를 켠 뒤에도 Django는 그대로 읽는다 (샌드박스 DB 계정은 슈퍼유저라 주인 여부와 상관없이 통과하므로,
        # Supabase에서는 배포 후 실제 화면으로 다시 확인한다)
        self.assertTrue(Lesson.objects.exists())


class LoggingSettingsTests(SimpleTestCase):
    def test_server_errors_are_logged_to_console(self):
        self.assertIn("console", settings.LOGGING["loggers"]["django"]["handlers"])
        self.assertEqual(settings.LOGGING["root"]["level"], "INFO")



# ---------------------------------------------------------------- 레슨 파일 (WBS 2.4)

GOOD_LESSON = '''
order_no = 1
track = "reading"
title = "테스트 레슨"
est_minutes = 8
published = true
body = "본문이에요."

[[questions]]
qtype = "predict"
prompt = "무엇이 나올까요?"
code = """
total = 0
for n in [1, 2, 3]:
    total += n
print(total)"""
choices = ["3", "6", "5", "0"]
answer = 2
explanation = "1+2+3이라 6이에요."

[[questions]]
qtype = "error"
prompt = "왜 에러가 날까요?"
code = """
row = {"부서명": "영업"}
print(row["부서"])"""
choices = ["열 이름이 없다", "한글이라서", "숫자라서", "파일이 없다"]
answer = 1
explanation = "부서라는 키가 없어요."
check = { error = "KeyError" }

[[questions]]
qtype = "fix_line"
prompt = "합계가 마지막 값만 나와요. 어디를 고칠까요?"
code = """
for n in [1, 2, 3]:
    total = 0
    total += n
print(total)"""
choices = ["1번째 줄", "2번째 줄을 반복문 위로 옮긴다", "3번째 줄", "4번째 줄"]
answer = 2
explanation = "2번째 줄이 반복마다 0으로 되돌려요."
check = { line = [1, 2], fix = "total = 0\\nfor n in [1, 2, 3]:", expect = "6", wrong = [ { choice = 4, line = 4, fix = "print(total + 1)" } ] }
'''


class LessonFileCheckTests(SimpleTestCase):
    """레슨 파일 검사기가 실제로 틀린 레슨을 잡는지 (python -m learning.lesson_files)"""

    def make(self, lesson_text, course_text='slug = "t"\ntitle = "테스트 과정"\npublished = true\n'):
        import tempfile
        from pathlib import Path

        root = Path(tempfile.mkdtemp())
        course = root / "t"
        course.mkdir()
        (course / "course.toml").write_text(course_text, encoding="utf-8")
        (course / "L01.toml").write_text(lesson_text, encoding="utf-8")
        return root

    def check(self, lesson_text, **kw):
        from .lesson_files import check_all

        return check_all(self.make(lesson_text), **kw)[1]

    def test_repository_lessons_pass(self):
        from .lesson_files import check_all

        courses, report = check_all()
        self.assertTrue(report.ok, report.errors)
        self.assertGreaterEqual(len(courses[0].lessons), 2)

    def test_good_lesson_runs_every_check(self):
        report = self.check(GOOD_LESSON)
        self.assertEqual(report.errors, [])
        text = "\n".join(report.lines)
        self.assertIn("실제 출력 '6' = 정답 2번", text)
        self.assertIn("KeyError: '부서'", text)
        self.assertIn("고친 뒤 '6'", text)
        self.assertIn("오답 4번", text)

    def test_wrong_predict_answer_is_caught(self):
        report = self.check(GOOD_LESSON.replace("answer = 2\nexplanation = \"1+2+3", "answer = 3\nexplanation = \"1+2+3"))
        self.assertTrue(any("실제 출력 '6'" in e and "2번 선택지" in e for e in report.errors), report.errors)

    def test_wrong_error_type_is_caught(self):
        report = self.check(GOOD_LESSON.replace('error = "KeyError"', 'error = "TypeError"'))
        self.assertTrue(any("TypeError가 나야 하는데" in e for e in report.errors), report.errors)

    def test_fix_that_does_not_fix_is_caught(self):
        report = self.check(GOOD_LESSON.replace('expect = "6"', 'expect = "7"'))
        self.assertTrue(any("정답대로 고쳐도" in e for e in report.errors), report.errors)

    def test_wrong_choice_that_also_works_is_caught(self):
        report = self.check(GOOD_LESSON.replace('fix = "print(total + 1)"', 'fix = "print(6)"'))
        self.assertTrue(any("정답이 2개" in e for e in report.errors), report.errors)

    def test_phone_width_line_reference_and_choices(self):
        long_code = GOOD_LESSON.replace('print(row["부서"])', 'print(row["부서"], "아주아주아주아주아주아주아주긴줄")')
        self.assertTrue(any("잘리는 줄 [2]" in e for e in self.check(long_code, run=False).errors))

        bad_ref = GOOD_LESSON.replace('"4번째 줄"]', '"9번째 줄"]')
        self.assertTrue(any("'9번째 줄'" in e for e in self.check(bad_ref, run=False).errors))

        three = GOOD_LESSON.replace('choices = ["3", "6", "5", "0"]', 'choices = ["3", "6", "5"]')
        self.assertTrue(any("선택지는" in e for e in self.check(three, run=False).errors))

        same = GOOD_LESSON.replace('choices = ["3", "6", "5", "0"]', 'choices = ["3", "6", "6", "0"]')
        self.assertTrue(any("같은 내용" in e for e in self.check(same, run=False).errors))

    def test_unknown_key_is_caught(self):
        typo = GOOD_LESSON.replace("published = true", "publshed = true")
        self.assertTrue(any("publshed" in e for e in self.check(typo, run=False).errors))
        stray = GOOD_LESSON + '\n[check]\nerror = "KeyError"\n'
        self.assertTrue(any("'check'" in e for e in self.check(stray, run=False).errors))
        bad_check = GOOD_LESSON.replace('check = { error = "KeyError" }', 'check = { eror = "KeyError" }')
        self.assertTrue(any("check.eror" in e for e in self.check(bad_check, run=False).errors))

    def test_body_code_block_rules(self):
        wide = GOOD_LESSON.replace('body = "본문이에요."', 'body = """본문\n\n```\nx = "아주아주아주아주아주아주아주아주긴줄이에요"\n```"""')
        self.assertTrue(any("본문 코드 상자" in e for e in self.check(wide, run=False).errors))
        unclosed = GOOD_LESSON.replace('body = "본문이에요."', 'body = """본문\n\n```\nx = 1"""')
        self.assertTrue(any("짝이 맞지 않음" in e for e in self.check(unclosed, run=False).errors))
        ok = GOOD_LESSON.replace('body = "본문이에요."', 'body = """본문\n\n```\nx = 1\n```"""')
        self.assertEqual(self.check(ok, run=False).errors, [])

    def test_lesson_number_and_file_name(self):
        report = self.check(GOOD_LESSON.replace("order_no = 1", "order_no = 2"), run=False)
        self.assertTrue(any("1부터 빈칸 없이" in e for e in report.errors), report.errors)
        self.assertTrue(any("L02.toml이어야" in e for e in report.errors), report.errors)

    def test_skipped_question_fails_only_in_strict(self):
        from unittest import mock as _mock

        from . import lesson_files

        with _mock.patch.object(lesson_files, "_missing_module", return_value="pandas"):
            normal = self.check(GOOD_LESSON)
            strict = self.check(GOOD_LESSON, strict=True)
        self.assertTrue(normal.ok)
        self.assertEqual(len(normal.skipped), 3)
        self.assertFalse(strict.ok)

    def test_command_line_exit_code(self):
        from .lesson_files import main

        with mock.patch("sys.stdout"):
            self.assertEqual(main(["--dir", str(self.make(GOOD_LESSON))]), 0)
            bad = GOOD_LESSON.replace('error = "KeyError"', 'error = "TypeError"')
            self.assertEqual(main(["--dir", str(self.make(bad))]), 1)

    def test_files_match_sample_fixture(self):
        """L01·L02 파일과 테스트용 fixture 내용이 같아야 한다 (한쪽만 고치는 실수 방지)"""
        from pathlib import Path

        from .lesson_files import load_all

        lessons = {l.order_no: l for l in load_all()[0].lessons}
        data = json.loads((Path(__file__).parent / "fixtures" / "sample_lessons.json").read_text(encoding="utf-8"))
        lesson_no = {}
        for obj in data:
            f = obj["fields"]
            if obj["model"] == "learning.lesson":
                lesson_no[obj["pk"]] = f["order_no"]
                l = lessons[f["order_no"]]
                self.assertEqual((l.track, l.title, l.body_md, l.est_minutes, l.is_published),
                                 (f["track"], f["title"], f["body_md"], f["est_minutes"], f["is_published"]))
            elif obj["model"] == "learning.question":
                q = lessons[lesson_no[f["lesson"]]].questions[f["order_no"] - 1]
                self.assertEqual((q.qtype, q.prompt, q.code, q.choices, q.answer, q.explanation),
                                 (f["qtype"], f["prompt"], f["code"], f["choices"], f["answer"], f["explanation"]))


class SyncLessonsTests(TestCase):
    """python manage.py sync_lessons : 파일 → DB 반영. 풀이 기록은 지키고 바뀐 것만 고친다"""

    fixtures = ["sample_lessons"]

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path

        from .lesson_files import LESSONS_DIR

        self.root = Path(tempfile.mkdtemp())
        shutil.copytree(LESSONS_DIR / "python-automation-30", self.root / "python-automation-30")
        self.course_dir = self.root / "python-automation-30"
        # 저장소의 초안 레슨(L03~)은 빼고 fixture와 같은 L01·L02만 남긴다
        for p in list(self.course_dir.glob("L*.toml")) + list(self.course_dir.glob("diag_*.toml")):
            if p.name not in ("L01.toml", "L02.toml"):
                p.unlink()

    def sync(self, *extra):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("sync_lessons", "--dir", str(self.root), "--no-chunks", *extra, stdout=out)
        return out.getvalue()

    def edit(self, name, old, new):
        p = self.course_dir / name
        text = p.read_text(encoding="utf-8")
        self.assertIn(old, text)
        p.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_files_equal_current_db(self):
        self.assertIn("새로 0개, 바뀜 0개, 그대로 6개", self.sync())  # 레슨 2 + 문제 4

    def test_update_keeps_attempts(self):
        from .models import Question

        progress = LessonProgress.objects.create(guest_key="g", lesson_id=1)
        AnswerAttempt.objects.create(progress=progress, question_id=1, selected=2, is_correct=True)
        self.edit("L01.toml", 'title = "반복문은', 'title = "반복문은 꼭')
        self.edit("L01.toml", "영업팀에서 연차", "영업팀에서 근속")
        out = self.sync()
        self.assertIn("바뀜 2개", out)
        self.assertEqual(Lesson.objects.get(pk=1).title[:6], "반복문은 꼭")
        self.assertIn("근속", Question.objects.get(pk=1).prompt)
        self.assertEqual(AnswerAttempt.objects.filter(question_id=1).count(), 1)  # 기록이 그대로

    def test_new_draft_lesson_is_created_unpublished(self):
        (self.course_dir / "L03.toml").write_text(GOOD_LESSON.replace("order_no = 1", "order_no = 3")
                                                  .replace("published = true", "published = false"), encoding="utf-8")
        out = self.sync()
        self.assertIn("+ L03 테스트 레슨", out)
        lesson = Lesson.objects.get(order_no=3)
        self.assertFalse(lesson.is_published)
        self.assertEqual(lesson.questions.count(), 3)
        self.assertEqual(Lesson.objects.filter(is_published=True).count(), 2)  # 초안은 사용자에게 안 보임

    def test_removed_question(self):
        from .models import Question

        text = (self.course_dir / "L02.toml").read_text(encoding="utf-8")
        cut = text.index("[[questions]]", text.index("[[questions]]") + 1)
        (self.course_dir / "L02.toml").write_text(text[:cut], encoding="utf-8")
        # 2문제 규칙 때문에 형식 검사에서 멈추고 DB는 그대로
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.sync()
        self.assertTrue(Question.objects.filter(pk=4).exists())

    def test_extra_question_without_attempts_is_deleted_with_attempts_kept(self):
        from .models import Question

        lesson = Lesson.objects.get(pk=2)
        q = Question.objects.create(lesson=lesson, order_no=3, qtype="flow", prompt="p", choices=["a", "b", "c", "d"],
                                    answer=1, explanation="e")
        self.assertIn("파일에 없어 지움", self.sync())
        self.assertFalse(Question.objects.filter(pk=q.pk).exists())

        q = Question.objects.create(lesson=lesson, order_no=3, qtype="flow", prompt="p", choices=["a", "b", "c", "d"],
                                    answer=1, explanation="e")
        progress = LessonProgress.objects.create(guest_key="g", lesson=lesson)
        AnswerAttempt.objects.create(progress=progress, question=q, selected=1, is_correct=True)
        self.assertIn("푼 기록이 있어 그대로 둠", self.sync())
        self.assertTrue(Question.objects.filter(pk=q.pk).exists())

    def test_invalid_file_changes_nothing(self):
        from django.core.management.base import CommandError

        self.edit("L01.toml", 'title = "반복문은', 'title = "바뀐 제목 반복문은')
        self.edit("L02.toml", 'track = "error"', 'track = "errors"')
        with self.assertRaises(CommandError):
            self.sync()
        self.assertTrue(Lesson.objects.get(pk=1).title.startswith("반복문은"))

    def test_dry_run_saves_nothing(self):
        self.edit("L01.toml", 'title = "반복문은', 'title = "바뀐 제목 반복문은')
        self.assertIn("미리보기: 새로 0개, 바뀜 1개", self.sync("--dry-run"))
        self.assertTrue(Lesson.objects.get(pk=1).title.startswith("반복문은"))

    def test_chunks_step_runs_after_sync(self):
        """ingest_chunks는 바뀐 레슨만 임베딩하므로 매번 불러도 된다. 지난번 실패분도 다음 시작 때 채워진다"""
        from io import StringIO

        from django.core.management import call_command

        with mock.patch("learning.management.commands.sync_lessons.call_command") as ingest:
            call_command("sync_lessons", "--dir", str(self.root), stdout=StringIO())
            call_command("sync_lessons", "--dir", str(self.root), "--dry-run", stdout=StringIO())
            call_command("sync_lessons", "--dir", str(self.root), "--no-chunks", stdout=StringIO())
        self.assertEqual(ingest.call_count, 1)
        self.assertEqual(ingest.call_args.args[0], "ingest_chunks")

    def test_chunk_failure_does_not_stop_server_start(self):
        from io import StringIO

        from django.core.management import call_command

        self.edit("L01.toml", 'title = "반복문은', 'title = "바뀐 제목 반복문은')
        out = StringIO()
        with mock.patch("learning.management.commands.sync_lessons.call_command", side_effect=RuntimeError("키 없음")):
            call_command("sync_lessons", "--dir", str(self.root), stdout=out)
        self.assertIn("청크 갱신 실패", out.getvalue())


class LessonAdminReadOnlyTests(TestCase):
    """레슨 내용은 파일이 원본이라 관리자 화면에서는 보기만 한다"""

    fixtures = ["sample_lessons"]

    def test_lesson_pages_open_read_only(self):
        admin_user = get_user_model().objects.create_superuser("boss", "boss@example.com", "pw-unused-123")
        self.client.force_login(admin_user)
        base = f"/{settings.ADMIN_PATH}learning/lesson/"
        listing = self.client.get(base)
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, "lessons/ 폴더의 레슨 파일")
        page = self.client.get(f"{base}1/change/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="title"')  # 입력칸이 없다
        self.assertEqual(self.client.get(f"{base}add/").status_code, 403)


class DraftPreviewTests(ApiTestCase):
    """관리자는 공개 전 초안 레슨을 실제 화면으로 미리 풀고, 다른 사람에게는 보이지 않는다"""

    def setUp(self):
        from .models import Question

        self.draft = Lesson.objects.create(course_id=1, order_no=3, track="reading", title="초안 레슨",
                                           body_md="초안 본문이에요.", is_published=False)
        self.q = Question.objects.create(lesson=self.draft, order_no=1, qtype="predict", prompt="p", code="print(1)",
                                         choices=["1", "2", "3", "4"], answer=1, explanation="e")
        User = get_user_model()
        self.staff = User.objects.create_user("staff", "staff@example.com", "pw-unused-123", is_staff=True)
        self.member = User.objects.create_user("member", "member@example.com", "pw-unused-123")

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_staff_sees_draft_with_preview_flag(self):
        c = self.as_user(self.staff)
        res = c.get(f"/api/v1/lessons/{self.draft.id}")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["is_preview"])
        self.assertFalse(c.get("/api/v1/lessons/1").json()["is_preview"])

    def test_draft_hidden_from_members_and_guests(self):
        self.assertEqual(self.as_user(self.member).get(f"/api/v1/lessons/{self.draft.id}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/lessons/{self.draft.id}").status_code, 404)
        res = self.post(f"/api/v1/lessons/{self.draft.id}/progress", client=self.as_user(self.member))
        self.assertEqual(res.status_code, 404)

    def test_staff_can_solve_draft(self):
        c = self.as_user(self.staff)
        pid = self.start(self.draft.id, client=c)
        self.assertTrue(self.answer(pid, self.q.id, 1, client=c).json()["is_correct"])
        self.assertEqual(self.post(f"/api/v1/progress/{pid}/complete", client=c).status_code, 200)

    def test_draft_completion_not_counted_in_week(self):
        c = self.as_user(self.staff)
        pid = self.start(self.draft.id, client=c)
        self.answer(pid, self.q.id, 1, client=c)
        res = self.post(f"/api/v1/progress/{pid}/complete", client=c).json()
        self.assertEqual(res["week"]["done_days"], 0)

    def test_today_never_offers_draft(self):
        c = self.as_user(self.staff)
        for lesson_id in (1, 2):
            pid = self.start(lesson_id, client=c)
            for qid in LESSON_QUESTIONS[lesson_id]:
                self.answer(pid, qid, ANSWERS[qid], client=c)
            self.post(f"/api/v1/progress/{pid}/complete", client=c)
        self.assertIsNone(c.get("/api/v1/today").json()["lesson"])

    def test_hint_sources_include_own_draft_but_not_other_drafts(self):
        from io import StringIO

        from django.core.management import call_command

        from . import hints
        from .models import Question

        other = Lesson.objects.create(course_id=1, order_no=4, track="reading", title="다른 초안",
                                      body_md="다른 초안 본문이에요.", is_published=False)
        with mock.patch("learning.management.commands.ingest_chunks.embed_texts", side_effect=fake_embed):
            call_command("ingest_chunks", stdout=StringIO())
        hints._question_vectors.clear()
        with mock.patch("learning.hints.embed_texts", side_effect=fake_embed):
            draft_ids = {c.lesson_id for c in hints.find_sources(self.q, k=10)}
            public_ids = {c.lesson_id for c in hints.find_sources(Question.objects.get(pk=1), k=10)}
        self.assertIn(self.draft.id, draft_ids)
        self.assertNotIn(other.id, draft_ids)
        self.assertNotIn(self.draft.id, public_ids)

    def test_admin_list_links_to_app_preview(self):
        admin_user = get_user_model().objects.create_superuser("boss", "boss@example.com", "pw-unused-123")
        c = self.as_user(admin_user)
        page = c.get(f"/{settings.ADMIN_PATH}learning/lesson/")
        self.assertContains(page, f'href="/lessons/{self.draft.id}/"')
        self.assertContains(page, "미리보기")


def diag_text(letter, order, qtypes=None, answers=None):
    """테스트용 진단 파일: 기본은 결과 예측 10문제"""
    qtypes = qtypes or ["predict"] * 10
    answers = answers or [1, 2, 3, 4, 1, 2, 3, 4, 1, 2]
    parts = [f'kind = "diagnostic"\norder_no = {order}\ntrack = "reading"\ntitle = "진단 {letter}"\n'
             f'est_minutes = 10\npublished = false\nbody = "읽고 고르세요."\n']
    for i, (qt, ans) in enumerate(zip(qtypes, answers)):
        choices = ["a", "b", "c", "d"]
        choices[ans - 1] = str(i)
        extra = 'check = { run = false, why = "테스트" }\n' if qt == "flow" else ""
        parts.append(f'\n[[questions]]\nqtype = "{qt}"\nprompt = "무엇이 나올까요?"\ncode = "print({i})"\n'
                     f'choices = {json.dumps(choices)}\nanswer = {ans}\nexplanation = "{i}이 나와요."\n{extra}')
    return "".join(parts)


class DiagnosticFileTests(SimpleTestCase):
    """사전·사후 진단 A·B는 같은 모양의 평행 검사지여야 한다 (FR-03·FR-15)"""

    def check(self, files, run=False):
        import tempfile
        from pathlib import Path

        from .lesson_files import check_all

        root = Path(tempfile.mkdtemp())
        course = root / "t"
        course.mkdir()
        (course / "course.toml").write_text('slug = "t"\ntitle = "테스트"\n', encoding="utf-8")
        (course / "L01.toml").write_text(GOOD_LESSON, encoding="utf-8")
        for name, text in files.items():
            (course / name).write_text(text, encoding="utf-8")
        return check_all(root, run=run)[1]

    def test_repository_diagnostics_are_parallel(self):
        from .lesson_files import load_all

        lessons = load_all()[0].lessons
        diags = {l.path.name: l for l in lessons if l.kind == "diagnostic"}
        self.assertEqual(set(diags), {"diag_A.toml", "diag_B.toml"})
        self.assertEqual([q.qtype for q in diags["diag_A.toml"].questions],
                         [q.qtype for q in diags["diag_B.toml"].questions])

    def test_good_pair_passes_and_runs(self):
        report = self.check({"diag_A.toml": diag_text("A", 101), "diag_B.toml": diag_text("B", 102)}, run=True)
        self.assertEqual(report.errors, [])
        self.assertIn("진단 A", "\n".join(report.lines))

    def test_rules(self):
        def errors(files):
            return " / ".join(self.check(files).errors)

        b_flow = diag_text("B", 102, qtypes=["predict"] * 9 + ["flow"])
        self.assertIn("유형 순서가 다름", errors({"diag_A.toml": diag_text("A", 101), "diag_B.toml": b_flow}))
        self.assertIn("두 세트가 같이", errors({"diag_A.toml": diag_text("A", 101)}))
        self.assertIn("order_no는 101", errors({"diag_A.toml": diag_text("A", 5), "diag_B.toml": diag_text("B", 102)}))
        self.assertIn("diag_A.toml 또는", errors({"진단.toml": diag_text("A", 101)}))
        nine = diag_text("A", 101, qtypes=["predict"] * 9, answers=[1, 2, 3, 4, 1, 2, 3, 4, 1])
        self.assertIn("10문제여야", errors({"diag_A.toml": nine, "diag_B.toml": diag_text("B", 102)}))

    def test_answer_crowding_warning(self):
        crowded = diag_text("A", 101, answers=[2] * 10)
        report = self.check({"diag_A.toml": crowded, "diag_B.toml": diag_text("B", 102)})
        self.assertTrue(any("진단 A의 정답이 2번에 10/10" in w for w in report.warnings), report.warnings)

    def test_diagnostic_numbers_do_not_break_lesson_numbering(self):
        report = self.check({"diag_A.toml": diag_text("A", 101), "diag_B.toml": diag_text("B", 102)})
        self.assertFalse(any("빈칸 없이" in e for e in report.errors), report.errors)


class PhoneLinkTests(TestCase):
    """코드의 전화번호 모양 글자가 아이폰에서 전화 링크로 바뀌지 않게 (10/13 L12 점검)"""

    def test_format_detection_off(self):
        self.assertContains(self.client.get("/"), 'name="format-detection" content="telephone=no')


class PolicyPageTests(TestCase):
    """구글 로그인 앱 게시 조건: 개인정보처리방침·이용약관 공개, 홈에서 연결 (10/14)"""

    def test_policy_pages_public(self):
        for url, word in (("/privacy/", "개인정보처리방침"), ("/terms/", "이용약관")):
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, url)
            self.assertContains(res, word)
        self.assertContains(self.client.get("/privacy/"), "OpenAI")

    def test_home_links_to_policies_but_lesson_page_does_not(self):
        home = self.client.get("/")
        self.assertContains(home, 'href="/privacy/"')
        self.assertContains(home, 'href="/terms/"')
        self.assertNotContains(self.client.get("/lessons/1/"), 'class="site-footer"')


class WeeklyGoalAndRecordsTests(ApiTestCase):
    """5.1 주간 목표(FR-02)·이번 주 진행(FR-11)·학습 기록과 예상 완료일(FR-12)"""

    def setUp(self):
        self.user = self.make_user()
        self.client.force_login(self.user)

    def patch_me(self, data, client=None):
        client = client or self.client
        return client.patch("/api/v1/me", json.dumps(data), content_type="application/json")

    def done_at(self, lesson_id, when, correct=2):
        p = LessonProgress.objects.create(user=self.user, lesson_id=lesson_id)
        LessonProgress.objects.filter(id=p.id).update(completed_at=when, correct_count=correct)

    def test_first_login_needs_goal_then_goal_is_used(self):
        body = self.client.get("/api/v1/today").json()
        self.assertTrue(body["needs_onboarding"])
        self.assertEqual(body["week"]["goal_days"], 5)  # 고르기 전 기본값

        res = self.patch_me({"weekly_goal_days": 3})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["weekly_goal_days"], 3)
        self.assertTrue(res.json()["onboarded"])

        body = self.client.get("/api/v1/today").json()
        self.assertFalse(body["needs_onboarding"])
        self.assertEqual(body["week"], {"done_days": 0, "goal_days": 3})
        _, done = self.solve_all(1)
        self.assertEqual(done["week"], {"done_days": 1, "goal_days": 3})

    def test_goal_can_change_later_without_resetting_onboarding(self):
        self.patch_me({"weekly_goal_days": 3})
        first = Profile.objects.get(user=self.user).onboarded_at
        self.assertEqual(self.patch_me({"weekly_goal_days": 7}).json()["weekly_goal_days"], 7)
        self.assertEqual(Profile.objects.get(user=self.user).onboarded_at, first)
        self.assertEqual(self.client.get("/api/v1/me").json()["weekly_goal_days"], 7)

    def test_goal_validation(self):
        for bad in [4, 0, "5", True, None]:
            res = self.patch_me({"weekly_goal_days": bad})
            self.assertEqual(res.status_code, 400, bad)
        self.assertEqual(self.patch_me({"weekly_goal_days": 5, "is_staff": True}).status_code, 400)
        self.assertFalse(get_user_model().objects.get(id=self.user.id).is_staff)
        self.assertIsNone(Profile.objects.get(user=self.user).onboarded_at)

    def test_guest_gets_401_and_no_onboarding(self):
        guest = Client()
        for url in ["/api/v1/me", "/api/v1/progress/weekly", "/api/v1/progress/lessons"]:
            self.assertEqual(guest.get(url).status_code, 401, url)
        self.assertEqual(self.patch_me({"weekly_goal_days": 3}, client=guest).status_code, 401)
        self.assertFalse(guest.get("/api/v1/today").json()["needs_onboarding"])

    def test_patch_needs_csrf_token(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(self.patch_me({"weekly_goal_days": 3}, client=strict).status_code, 403)

    def test_weekly_counts_last_four_weeks_by_korean_date(self):
        self.patch_me({"weekly_goal_days": 3})
        utc = dt_timezone.utc
        # 지금: 한국 10/14(수) 15:00. 이번 주 월요일 = 10/12
        now = datetime(2026, 10, 14, 6, 0, tzinfo=utc)
        self.done_at(1, datetime(2026, 10, 12, 1, 0, tzinfo=utc))   # 이번 주 월
        self.done_at(2, datetime(2026, 10, 12, 2, 0, tzinfo=utc))   # 같은 날 → 1일
        self.done_at(1, datetime(2026, 10, 11, 16, 0, tzinfo=utc))  # 한국 10/12(월) 01:00 → 이번 주
        self.done_at(1, datetime(2026, 10, 5, 1, 0, tzinfo=utc))    # 지난주 3일
        self.done_at(1, datetime(2026, 10, 7, 1, 0, tzinfo=utc))
        self.done_at(2, datetime(2026, 10, 9, 1, 0, tzinfo=utc))
        self.done_at(1, datetime(2026, 9, 1, 1, 0, tzinfo=utc))     # 4주보다 전 → 안 셈
        with mock.patch("django.utils.timezone.now", return_value=now):
            weeks = self.client.get("/api/v1/progress/weekly?weeks=4").json()["weeks"]
        self.assertEqual([w["week_start"] for w in weeks], ["2026-10-12", "2026-10-05", "2026-09-28", "2026-09-21"])
        self.assertEqual([w["done_days"] for w in weeks], [1, 3, 0, 0])
        self.assertEqual([w["achieved"] for w in weeks], [False, True, False, False])
        self.assertTrue(all(w["goal_days"] == 3 for w in weeks))

    def test_weekly_validates_weeks(self):
        for bad in ["0", "13", "abc"]:
            self.assertEqual(self.client.get(f"/api/v1/progress/weekly?weeks={bad}").status_code, 400, bad)

    def test_lesson_records_latest_only_and_estimate(self):
        utc = dt_timezone.utc
        now = datetime(2026, 10, 14, 6, 0, tzinfo=utc)  # 한국 10/14
        self.done_at(1, datetime(2026, 10, 12, 1, 0, tzinfo=utc), correct=1)
        self.done_at(1, datetime(2026, 10, 13, 15, 30, tzinfo=utc), correct=2)  # 다시 풂: 한국 10/14 00:30
        LessonProgress.objects.create(user=self.user, lesson_id=2)  # 시작만 하고 안 끝냄
        with mock.patch("django.utils.timezone.now", return_value=now):
            body = self.client.get("/api/v1/progress/lessons").json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["remaining"], 1)
        self.assertEqual(len(body["lessons"]), 1)
        rec = body["lessons"][0]
        self.assertEqual((rec["id"], rec["correct_count"], rec["question_count"]), (1, 2, 2))
        self.assertTrue(rec["completed_at"].startswith("2026-10-14T00:30"))  # 한국 시간으로
        # 남은 1개 ÷ 주 5일 → 7/5일 올림 = 2일 뒤
        self.assertEqual(body["estimate"], {"days": 2, "date": "2026-10-16"})

        self.patch_me({"weekly_goal_days": 3})
        with mock.patch("django.utils.timezone.now", return_value=now):
            self.assertEqual(self.client.get("/api/v1/progress/lessons").json()["estimate"]["days"], 3)

    def test_estimate_none_when_all_done_and_math(self):
        from .views import estimate_finish

        self.assertIsNone(estimate_finish(0, 5))
        self.assertEqual(estimate_finish(14, 7)["days"], 14)
        self.assertEqual(estimate_finish(14, 5)["days"], 20)  # 14×7÷5 = 19.6 → 20
        self.assertEqual(estimate_finish(14, 3)["days"], 33)  # 32.67 → 33
        self.solve_all(1)
        self.solve_all(2)
        body = self.client.get("/api/v1/progress/lessons").json()
        self.assertEqual((body["remaining"], body["estimate"]), (0, None))

    def test_records_exclude_drafts_and_diagnostics(self):
        lesson = Lesson.objects.get(id=1)
        draft = Lesson.objects.create(course=lesson.course, order_no=50, track="reading",
                                      title="초안", body_md="본문", is_published=False)
        diag = Lesson.objects.create(course=lesson.course, order_no=101, kind="diagnostic", track="reading",
                                     title="진단 A", body_md="본문", is_published=True)
        now = timezone.now()
        self.done_at(draft.id, now)
        self.done_at(diag.id, now)
        body = self.client.get("/api/v1/progress/lessons").json()
        self.assertEqual((body["total"], body["lessons"]), (2, []))
        # 진단은 공부한 날로 세고, 초안 미리보기는 세지 않는다
        self.assertEqual(self.client.get("/api/v1/today").json()["week"]["done_days"], 1)

    def test_records_page_requires_login_and_links_exist(self):
        res = Client().get("/records/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/accounts/login/", res["Location"])
        res = self.client.get("/records/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'data-page="records"')
        self.assertIn("csrftoken", res.cookies)
        self.assertContains(self.client.get("/"), 'href="/records/"')  # 상단 '기록' 링크


class GoogleAccountChooserTests(TestCase):
    """구글 로그인은 매번 계정 고르는 화면을 띄운다 (10/14 폰 점검: 다른 계정으로 못 바꿈)"""

    def test_google_login_asks_to_select_account(self):
        from urllib.parse import parse_qs, urlparse

        res = self.client.post("/accounts/google/login/")
        self.assertEqual(res.status_code, 302)
        url = urlparse(res["Location"])
        self.assertEqual(url.netloc, "accounts.google.com")
        self.assertEqual(parse_qs(url.query)["prompt"], ["select_account"])
