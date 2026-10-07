"""python manage.py ingest_chunks : 레슨 본문을 잘라 임베딩하고 lesson_chunk에 저장한다 (FR-17, WBS 4.1)

- 바뀐 레슨만 다시 만든다 (제목+본문 지문이 같으면 건너뜀). 전부 다시: --all
- 무엇이 만들어질지만 보기 (API 호출·저장 없음): --dry-run
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from learning.chunks import source_hash, split_into_chunks, text_for_embedding
from learning.embeddings import EmbeddingError, embed_texts
from learning.models import Lesson, LessonChunk


class Command(BaseCommand):
    help = "레슨 본문을 힌트 검색용 청크로 잘라 임베딩해 저장한다"

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="바뀌지 않은 레슨도 전부 다시 만든다")
        parser.add_argument("--dry-run", action="store_true", help="잘린 결과만 보여 주고 저장하지 않는다")

    def handle(self, *args, **options):
        rebuild_all, dry_run = options["all"], options["dry_run"]
        done = skipped = chunk_total = token_total = 0

        for lesson in Lesson.objects.select_related("course").order_by("course_id", "order_no"):
            digest = source_hash(lesson.title, lesson.body_md)
            existing = set(lesson.chunks.values_list("source_hash", flat=True))
            if not rebuild_all and existing == {digest}:
                skipped += 1
                continue

            pieces = split_into_chunks(lesson.body_md)
            if dry_run:
                self.stdout.write(f"\n[{lesson.order_no}] {lesson.title} → {len(pieces)}개")
                for i, piece in enumerate(pieces):
                    self.stdout.write(f"  {i}. ({len(piece)}자) {piece}")
                chunk_total += len(pieces)
                done += 1
                continue

            if not pieces:
                lesson.chunks.all().delete()
                done += 1
                continue

            try:
                vectors, tokens = embed_texts([text_for_embedding(lesson.title, p) for p in pieces])
            except EmbeddingError as e:
                raise CommandError(str(e))

            # 지우고 새로 넣기를 한 번에: 중간에 실패하면 예전 청크가 그대로 남는다
            with transaction.atomic():
                lesson.chunks.all().delete()
                LessonChunk.objects.bulk_create(
                    LessonChunk(lesson=lesson, chunk_index=i, content=p, embedding=v, source_hash=digest)
                    for i, (p, v) in enumerate(zip(pieces, vectors))
                )
            self.stdout.write(f"[{lesson.order_no}] {lesson.title}: 청크 {len(pieces)}개")
            done += 1
            chunk_total += len(pieces)
            token_total += tokens

        label = "미리보기" if dry_run else "저장"
        self.stdout.write(self.style.SUCCESS(
            f"\n{label} 완료: 레슨 {done}개 처리, {skipped}개는 바뀐 게 없어 건너뜀, 청크 {chunk_total}개"
            + ("" if dry_run else f", 임베딩 토큰 {token_total}개")
        ))
