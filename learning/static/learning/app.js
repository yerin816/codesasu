/* 코드사수 화면 스크립트
 * - 데이터는 모두 /api/v1/ 에서 받는다. 정답 채점은 서버가 한다.
 * - API에서 받은 글자는 반드시 esc()로 바꾼 뒤 화면에 넣는다 (스크립트 삽입 방지).
 */
"use strict";

const TRACK = { reading: "읽기", error: "에러", shipping: "내보내기" };
const QTYPE = { predict: "결과 예측", error: "에러 해석", fix_line: "고칠 곳 고르기", flow: "흐름 설명" };

const $ = (sel) => document.querySelector(sel);

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

/* 레슨 본문용 아주 작은 마크다운: 문단, `코드`, **굵게** 만 지원한다 (새 라이브러리 없이) */
function inline(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\n/g, "<br>");
}
function markdown(md) {
  // ``` 로 감싼 줄은 본문 속 코드 상자로, 나머지는 빈 줄 기준 문단으로 (10/12 폰 점검: 긴 코드가 문장 속에서 쪼개짐)
  return String(md || "")
    .split(/```[^\n]*\n([\s\S]*?)\n?```/)
    .map((part, i) => (i % 2 === 1
      ? `<pre class="code snippet">${esc(part)}</pre>`
      : part.split(/\n{2,}/).filter((p) => p.trim()).map((p) => `<p>${inline(p.trim())}</p>`).join("")))
    .join("");
}

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

/* API 호출. 실패해도 예외 대신 {ok:false, message}를 돌려줘 화면이 멈추지 않게 한다 */
async function api(path, body, method) {
  const options = { method: method || (body === undefined ? "GET" : "POST"), credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRFToken"] = csrfToken();
    options.body = JSON.stringify(body);
  }
  try {
    const res = await fetch(`/api/v1/${path}`, options);
    const data = await res.json().catch(() => ({}));
    const message = (data.error && data.error.message) || "잠시 후 다시 시도해 주세요.";
    return { ok: res.ok, status: res.status, data, message };
  } catch (e) {
    return { ok: false, status: 0, data: {}, message: "인터넷 연결을 확인하고 다시 시도해 주세요." };
  }
}

function weekHtml(week) {
  let dots = "";
  for (let i = 0; i < week.goal_days; i++) {
    dots += `<span class="dot${i < week.done_days ? " on" : ""}"></span>`;
  }
  // 연속 일수는 보여주지 않는다 (FR-11: 하루 빠져도 벌 주지 않기)
  return `<p class="eyebrow">이번 주 목표</p>
    <p class="week-count"><strong>${week.done_days}</strong> / ${week.goal_days}일</p>
    <div class="dots" aria-hidden="true">${dots}</div>`;
}

/* 주간 목표 고르기 (FR-02). 처음 로그인한 홈과 학습 기록 화면에서 같이 쓴다 */
const GOALS = [
  { days: 3, label: "주 3일", note: "가볍게" },
  { days: 5, label: "주 5일", note: "평일마다" },
  { days: 7, label: "매일", note: "습관 만들기" },
];

function goalPickerHtml(current) {
  return `<div class="goal-picker" role="group" aria-label="주간 목표">${GOALS.map((g) =>
    `<button type="button" class="goal-opt" data-days="${g.days}" aria-pressed="${g.days === current}">
      <strong>${g.label}</strong><span>${g.note}</span></button>`).join("")}</div>`;
}

/* 버튼을 누르면 저장하고 onSaved(설정)를 부른다. 실패하면 msgEl에 안내 */
function bindGoalPicker(root, msgEl, onSaved) {
  root.querySelectorAll(".goal-opt").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (root.dataset.busy) return;
      root.dataset.busy = "1";
      const res = await api("me", { weekly_goal_days: Number(btn.dataset.days) }, "PATCH");
      delete root.dataset.busy;
      if (!res.ok) {
        msgEl.innerHTML = `<p class="error small">${esc(res.message)}</p>`;
        return;
      }
      onSaved(res.data);
    });
  });
}

function shortDate(iso) {
  const [, m, d] = String(iso).slice(0, 10).split("-").map(Number);
  return `${m}월 ${d}일`;
}

function minutesText(sec) {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m > 0 ? `${m}분 ${s}초` : `${s}초`;
}

/* ---------------- 홈 ---------------- */

async function initHome() {
  const box = $("#today");
  const res = await api("today");
  if (!res.ok) {
    box.innerHTML = `<p class="error">${esc(res.message)}</p>`;
    return;
  }
  const { lesson, week, is_guest, needs_onboarding, pre_diagnostic } = res.data;

  if (needs_onboarding) {
    // 처음 로그인: 레슨보다 주간 목표를 먼저 고른다 (FR-02)
    box.innerHTML = `<p class="eyebrow">시작하기 전에</p>
      <h2>일주일에 며칠 풀어 볼까요?</h2>
      <p class="muted small">하루에 레슨 1개, 10분 정도예요. 목표는 학습 기록 화면에서 언제든 바꿀 수 있어요.</p>
      ${goalPickerHtml(null)}
      <div id="goal-msg"></div>`;
    bindGoalPicker(box, $("#goal-msg"), () => initHome());
    $("#week").hidden = true;
    return;
  }

  if (pre_diagnostic) {
    // 목표를 고른 뒤 첫 레슨 전에 사전 진단 (FR-03). 건너뛸 수 있지만 그러면 베타 비교에서 빠진다
    box.innerHTML = `<p class="eyebrow">시작하기 전에</p>
      <h2>지금 코드 읽기 실력부터 재 볼까요?</h2>
      <p class="muted small">10문제 · 약 ${esc(pre_diagnostic.est_minutes)}분. 문제마다 한 번씩 답하고, 맞았는지는 다 풀고 나서 알려 드려요.
        베타 마지막 주에 다른 문제로 한 번 더 재서 얼마나 늘었는지 비교해 드려요.</p>
      <a class="btn primary" href="/lessons/${encodeURIComponent(pre_diagnostic.id)}/">진단 시작하기</a>
      <button type="button" class="btn ghost" id="skip-diag">레슨부터 할게요</button>
      <p class="muted small center">건너뛰면 나중에 실력 비교 결과를 볼 수 없어요.</p>
      <div id="diag-msg"></div>`;
    $("#skip-diag").addEventListener("click", async (e) => {
      e.target.disabled = true;
      const r = await api("diagnostics/pre/skip", {});
      if (!r.ok) {
        e.target.disabled = false;
        $("#diag-msg").innerHTML = `<p class="error small">${esc(r.message)}</p>`;
        return;
      }
      initHome();
    });
  } else if (!lesson) {
    box.innerHTML = `<h2>준비된 레슨을 모두 끝냈어요</h2>
      <p class="muted">새 레슨이 올라오면 여기에 보여 드릴게요.</p>`;
  } else {
    box.innerHTML = `<p class="tag">${esc(TRACK[lesson.track] || "")} · 약 ${esc(lesson.est_minutes)}분</p>
      <h2>${esc(lesson.title)}</h2>
      <a class="btn primary" href="/lessons/${encodeURIComponent(lesson.id)}/">${is_guest ? "가입 없이 첫 레슨 풀어보기" : "시작하기"}</a>
      ${is_guest ? '<p class="muted small">첫 레슨은 가입 없이 풀 수 있어요.</p>' : ""}`;
  }

  if (week) {
    const weekBox = $("#week");
    weekBox.innerHTML = `${weekHtml(week)}<a class="more-link" href="/records/">학습 기록 보기</a>`;
    weekBox.hidden = false;
  }
}

/* ---------------- 학습 기록 (FR-12) ---------------- */

async function initRecords() {
  const finish = $("#finish");
  const [lessonsRes, weeksRes, meRes] = await Promise.all([
    api("progress/lessons"), api("progress/weekly?weeks=4"), api("me"),
  ]);
  if (!lessonsRes.ok || !weeksRes.ok || !meRes.ok) {
    const failed = [lessonsRes, weeksRes, meRes].find((r) => !r.ok);
    finish.innerHTML = `<p class="error">${esc(failed.message)}</p>`;
    return;
  }
  const rec = lessonsRes.data;
  const done = rec.total - rec.remaining;
  const pct = rec.total ? Math.round((done / rec.total) * 100) : 0;

  // 1) 예상 완료일: 남은 레슨 ÷ 주간 목표
  let eta;
  if (!rec.estimate) {
    eta = `<h2>레슨 ${esc(rec.total)}개를 모두 끝냈어요</h2>`;
  } else {
    const days = rec.estimate.days;
    const when = days >= 14 ? `약 ${Math.round(days / 7)}주 뒤` : `약 ${days}일 뒤`;
    eta = `<h2>${esc(shortDate(rec.estimate.date))} 무렵 끝나요</h2>
      <p class="muted small">남은 레슨 ${esc(rec.remaining)}개 · 주 ${esc(meRes.data.weekly_goal_days)}일씩 풀면 ${esc(when)}</p>`;
  }
  finish.innerHTML = `<p class="eyebrow">예상 완료일</p>${eta}
    <div class="bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"
      aria-label="끝낸 레슨 ${done}개 / ${rec.total}개"><span style="width:${pct}%"></span></div>
    <p class="muted small">끝낸 레슨 ${esc(done)} / ${esc(rec.total)}개</p>`;

  // 2) 주별 기록(최근 4주, 가입한 주부터): 이번 주가 맨 위. 연속 기록은 보여주지 않는다 (FR-11)
  const weeksBox = $("#weeks");
  weeksBox.innerHTML = `<p class="eyebrow">주별 기록</p>
    <ul class="week-list">${weeksRes.data.weeks.map((w, i) => {
      let dots = "";
      for (let k = 0; k < w.goal_days; k++) dots += `<span class="dot${k < w.done_days ? " on" : ""}"></span>`;
      return `<li>
        <span class="wk">${i === 0 ? "이번 주" : `${esc(shortDate(w.week_start))} 주`}</span>
        <span class="dots mini" aria-hidden="true">${dots}</span>
        <span class="wk-count">${esc(w.done_days)}/${esc(w.goal_days)}일${w.achieved ? ' <b class="ok">달성</b>' : ""}</span>
      </li>`;
    }).join("")}</ul>`;
  weeksBox.hidden = false;

  // 3) 주간 목표 바꾸기
  const goalBox = $("#goal");
  goalBox.innerHTML = `<p class="eyebrow">주간 목표</p>
    ${goalPickerHtml(meRes.data.weekly_goal_days)}<div id="goal-msg"></div>`;
  bindGoalPicker(goalBox, $("#goal-msg"), () => initRecords());
  goalBox.hidden = false;

  // 4) 끝낸 레슨 (같은 레슨을 다시 풀었으면 최근 기록)
  const listBox = $("#done-lessons");
  listBox.innerHTML = `<p class="eyebrow">끝낸 레슨</p>${rec.lessons.length
    ? `<ul class="lesson-list">${rec.lessons.map((l) => `<li>
        <a href="/lessons/${encodeURIComponent(l.id)}/">
          <span class="ln-no">L${esc(String(l.order_no).padStart(2, "0"))}</span>
          <span class="ln-title">${esc(l.title)}</span>
          <span class="ln-meta">${esc(TRACK[l.track] || "")} · 처음에 맞힘 ${esc(l.correct_count)}/${esc(l.question_count)} · ${esc(shortDate(l.completed_at.slice(0, 10)))}</span>
        </a></li>`).join("")}</ul>`
    : '<p class="muted small">아직 끝낸 레슨이 없어요. 홈에서 오늘의 레슨을 시작해 보세요.</p>'}`;
  listBox.hidden = false;
}

/* ---------------- 레슨 풀이 ---------------- */

const state = { lesson: null, idx: 0, progressId: null, selected: null, busy: false, hints: [], hintBusy: false };
const HINT_LEVEL = { 1: "방향", 2: "위치", 3: "개념" };

function setActions(buttons) {
  // buttons: [{label, kind, onClick, disabled}]
  const bar = $("#actionbar");
  bar.innerHTML = "";
  buttons.forEach((b) => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = `btn ${b.kind || "primary"}`;
    el.textContent = b.label;
    el.disabled = !!b.disabled;
    if (b.id) el.id = b.id;
    el.addEventListener("click", b.onClick);
    bar.appendChild(el);
  });
  bar.hidden = buttons.length === 0;
}

/* 채점 결과가 선택지 아래에 생겨 폰에서는 화면 밖일 수 있어, 보이는 곳으로 내려 준다 */
function revealFeedback() {
  const fb = $("#q-feedback");
  fb.hidden = false;
  fb.scrollIntoView({ behavior: "smooth", block: "center" });
}

function showError(message) {
  const fb = $("#q-feedback");
  fb.className = "feedback error";
  fb.innerHTML = `<p>${esc(message)}</p>`;
  fb.hidden = false;
}

async function initLesson() {
  const lessonId = $("#lesson").dataset.lessonId;
  const intro = $("#intro");
  const res = await api(`lessons/${lessonId}`);

  if (res.status === 401) {
    const next = encodeURIComponent(location.pathname);
    intro.innerHTML = `<h1>로그인하면 이어서 풀 수 있어요</h1>
      <p class="muted">첫 레슨 기록도 계정에 함께 저장돼요.</p>
      <a class="btn primary" href="/accounts/login/?next=${next}">로그인하기</a>`;
    return;
  }
  if (!res.ok) {
    intro.innerHTML = `<p class="error">${esc(res.message)}</p><a class="btn ghost" href="/">홈으로</a>`;
    return;
  }

  state.lesson = res.data;
  const l = state.lesson;
  const preview = l.is_preview
    ? `<p class="preview-note">미리보기: 공개 전 초안이에요. 관리자에게만 보여요.</p>` : "";
  const tag = l.is_diagnostic ? "사전 진단" : (TRACK[l.track] || "");
  intro.innerHTML = `${preview}<p class="tag">${esc(tag)} · 약 ${esc(l.est_minutes)}분</p>
    <h1>${esc(l.title)}</h1>
    <div class="body">${markdown(l.body_md)}</div>`;
  fitSnippets();
  setActions([{ label: `${l.is_diagnostic ? "진단 시작" : "문제 풀기"} (${l.questions.length}문제)`, onClick: startQuiz }]);
}

async function startQuiz() {
  if (state.busy) return;
  state.busy = true;
  const res = await api(`lessons/${state.lesson.id}/progress`, {});
  state.busy = false;
  if (!res.ok) {
    $("#intro").insertAdjacentHTML("beforeend", `<p class="error">${esc(res.message)}</p>`);
    return;
  }
  state.progressId = res.data.progress_id;
  state.idx = 0;
  $("#intro").hidden = true;
  $("#quiz").hidden = false;
  showQuestion();
}

function currentQuestion() {
  return state.lesson.questions[state.idx];
}

function showQuestion() {
  const q = currentQuestion();
  const total = state.lesson.questions.length;
  state.selected = null;

  $("#q-count").textContent = `문제 ${state.idx + 1} / ${total} · ${QTYPE[q.qtype] || ""}`;
  $("#q-prompt").innerHTML = inline(q.prompt);
  const code = $("#q-code");
  // 줄 번호를 붙인다. 선택지·해설이 'N번째 줄'로 말하므로 사용자가 줄을 세지 않게 (10/12 폰 점검)
  code.innerHTML = (q.code || "").split("\n")
    .map((line, i) => `<span class="ln" aria-hidden="true">${i + 1}</span>${esc(line)}`)
    .join("\n");
  code.hidden = !q.code;
  code.scrollLeft = 0;
  fitCode();

  const list = $("#q-choices");
  list.innerHTML = "";
  q.choices.forEach((text, i) => {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "choice";
    btn.dataset.no = String(i + 1); // 선택지 번호는 1부터 (API와 같음)
    btn.setAttribute("aria-pressed", "false");
    btn.innerHTML = `<span class="no">${i + 1}</span><span class="text">${inline(text)}</span>`;
    btn.addEventListener("click", () => selectChoice(i + 1));
    li.appendChild(btn);
    list.appendChild(li);
  });

  $("#q-feedback").hidden = true;
  state.hints = [];
  closeSasu();
  const diag = state.lesson.is_diagnostic;
  $("#hint-btn").hidden = diag; // 진단에서는 사수 힌트를 쓰지 않는다
  const last = state.idx === total - 1;
  const label = diag ? (last ? "제출하고 결과 보기" : "제출하고 다음 문제") : "제출하기";
  setActions([{ label, id: "submit-btn", onClick: submitAnswer, disabled: true }]);
  window.scrollTo(0, 0);
}

/* 코드 줄이 폰 화면보다 길면 글자를 최소 CODE_MIN_PX까지 줄여 한 화면에 맞춘다.
 * 그래도 넘치면 가로 스크롤을 두고 오른쪽 흐림 + 안내 문구를 보여 준다.
 * (10/8 아이폰 확인: 13.5px에서 정답을 가르는 조건 `>= 5`가 화면 밖으로 잘려 안 보였다) */
const CODE_PX = 13.5;
const CODE_MIN_PX = 11.5;

function fitCode() {
  const code = $("#q-code");
  const wrap = $("#q-code-wrap");
  const hint = $("#q-code-hint");
  if (!code || !wrap || !hint) return;
  code.style.fontSize = "";
  if (code.hidden || !code.textContent) {
    wrap.classList.remove("is-overflow");
    hint.hidden = true;
    return;
  }
  const style = getComputedStyle(code);
  const pad = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
  const box = code.clientWidth - pad;
  const full = code.scrollWidth - pad;
  if (full > box) {
    const size = Math.max(CODE_MIN_PX, Math.floor((CODE_PX * box / full) * 10) / 10);
    code.style.fontSize = `${size}px`;
  }
  const overflow = code.scrollWidth > code.clientWidth + 1;
  wrap.classList.toggle("is-overflow", overflow);
  wrap.classList.remove("at-end");
  hint.hidden = !overflow;
}

/* 본문 속 코드 상자도 폰 폭에 맞게 글자를 줄인다 (10/13 폰 점검: 에러 문구 한 줄이 360~390px에서 넘침) */
const SNIPPET_PX = 13;
const SNIPPET_MIN_PX = 11;
function fitSnippets() {
  document.querySelectorAll("pre.snippet").forEach((pre) => {
    pre.style.fontSize = "";
    const style = getComputedStyle(pre);
    const pad = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const box = pre.clientWidth - pad;
    const full = pre.scrollWidth - pad;
    if (full > box) {
      pre.style.fontSize = `${Math.max(SNIPPET_MIN_PX, Math.floor((SNIPPET_PX * box / full) * 10) / 10)}px`;
    }
  });
}

function markCodeScrollEnd() {
  const code = $("#q-code");
  const wrap = $("#q-code-wrap");
  if (!code || !wrap) return;
  wrap.classList.toggle("at-end", code.scrollLeft + code.clientWidth >= code.scrollWidth - 2);
}

function choiceButtons() {
  return Array.from(document.querySelectorAll("#q-choices .choice"));
}

function selectChoice(no) {
  // 힌트를 읽고 답을 고르면 시트를 접어 선택지와 제출 버튼이 보이게 한다
  if (!$("#sasu").hidden) setSasuMini(true);
  state.selected = no;
  choiceButtons().forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.no) === no)));
  const submit = $("#submit-btn");
  if (submit) submit.disabled = false;
}

function lockChoices() {
  choiceButtons().forEach((b) => (b.disabled = true));
}

function nextAction() {
  const last = state.idx === state.lesson.questions.length - 1;
  return { label: last ? "결과 보기" : "다음 문제", onClick: last ? finishLesson : nextQuestion };
}

async function submitAnswer() {
  if (state.busy || state.selected == null) return;
  state.busy = true;
  const q = currentQuestion();
  const picked = state.selected;
  const res = await api(`progress/${state.progressId}/answers`, { question_id: q.id, selected: picked });
  state.busy = false;
  if (!res.ok) {
    showError(res.message);
    return;
  }

  if (state.lesson.is_diagnostic) {
    // 진단: 맞았는지 보여 주지 않고 바로 다음 문제로 (FR-03)
    lockChoices();
    nextAction().onClick();
    return;
  }

  const btn = choiceButtons().find((b) => Number(b.dataset.no) === picked);
  const fb = $("#q-feedback");

  if (res.data.is_correct) {
    btn.classList.add("correct");
    lockChoices();
    endHints();
    fb.className = "feedback correct";
    fb.innerHTML = `<p class="verdict">정답이에요</p><div class="explain">${markdown(res.data.explanation)}</div>`;
    revealFeedback();
    setActions([nextAction()]);
    return;
  }

  // 틀렸을 때: 해설(=정답)은 주지 않고, 다시 풀기와 해설 보기 중 고르게 한다 (10/7 결정 A)
  btn.classList.add("wrong");
  btn.disabled = true; // 이미 고른 오답은 다시 못 고르게
  btn.setAttribute("aria-pressed", "false");
  state.selected = null;
  fb.className = "feedback wrong";
  fb.innerHTML = `<p class="verdict">아쉬워요, 다시 생각해 볼까요?</p>
    <p class="muted small">다른 답을 골라 보거나, 해설을 보고 넘어갈 수 있어요.</p>`;
  revealFeedback();
  // FR-09: 같은 문제를 두 번 연속 틀렸고 아직 힌트를 안 받았으면 사수가 먼저 제안한다
  if (res.data.suggest_hint && state.hints.length === 0) offerHint();
  setActions([
    { label: "해설 보기", kind: "ghost", onClick: revealExplanation },
    { label: "다시 제출", id: "submit-btn", onClick: submitAnswer, disabled: true },
  ]);
}

async function revealExplanation() {
  if (state.busy) return;
  state.busy = true;
  const q = currentQuestion();
  const res = await api(`progress/${state.progressId}/explanation`, { question_id: q.id });
  state.busy = false;
  if (!res.ok) {
    showError(res.message);
    return;
  }
  const answerBtn = choiceButtons().find((b) => Number(b.dataset.no) === res.data.answer);
  if (answerBtn) answerBtn.classList.add("correct");
  lockChoices();
  endHints();
  const fb = $("#q-feedback");
  fb.className = "feedback reveal";
  fb.innerHTML = `<p class="verdict">정답은 ${esc(res.data.answer)}번이에요</p><div class="explain">${markdown(res.data.explanation)}</div>`;
  revealFeedback();
  setActions([nextAction()]);
}

/* ---------------- 사수 힌트 (FR-08, FR-09) ---------------- */

function openSasu(html) {
  $("#sasu-body").innerHTML = html;
  $("#sasu").hidden = false;
  setSasuMini(false);
}

function closeSasu() {
  $("#sasu").hidden = true;
  updateSasuSpace();
}

/* 사수 시트 접기·펼치기. 접으면 머리 한 줄만 남는다 (10/14: 시트가 선택지를 가림) */
function setSasuMini(mini) {
  const sheet = $("#sasu");
  sheet.classList.toggle("is-mini", mini);
  const toggle = $("#sasu-toggle");
  toggle.textContent = mini ? "펼치기" : "접기";
  toggle.setAttribute("aria-expanded", String(!mini));
  const n = state.hints.length;
  $("#sasu-summary").textContent = n ? `힌트 ${n}단계까지 받았어요` : "할 말이 있어요";
  updateSasuSpace();
}

/* 폰에서는 시트가 화면 아래를 덮으므로, 그 높이만큼 페이지 아래 여백을 늘려 선택지를 시트 위로 올릴 수 있게 한다.
 * 넓은 PC(1340px~)는 시트가 본문 옆에 있어 필요 없다 */
function updateSasuSpace() {
  const sheet = $("#sasu");
  const beside = window.matchMedia("(min-width: 1340px)").matches;
  const h = !sheet || sheet.hidden || beside ? 0 : sheet.offsetHeight;
  document.documentElement.style.setProperty("--sasu-space", `${h}px`);
}

/* 맞혔거나 해설을 봤으면 힌트는 끝 */
function endHints() {
  $("#hint-btn").hidden = true;
  closeSasu();
}

function offerHint() {
  openSasu(`<p class="sasu-msg">두 번 연속 틀렸네요. 괜찮아요, 어디를 보면 좋을지 살짝 짚어 드릴까요?</p>
    <button type="button" class="btn ghost small" id="sasu-next">힌트 받기</button>`);
  $("#sasu-next").addEventListener("click", requestHint);
}

function renderHints(remaining) {
  const items = state.hints.map((h) => {
    const sources = (h.sources || []).map((s) => `'${esc(s.title)}'`).join(", ");
    return `<div class="sasu-item">
        <p class="sasu-level">${h.level}단계 · ${HINT_LEVEL[h.level]}</p>
        <p class="sasu-msg">${inline(h.message)}</p>
        ${sources ? `<p class="sasu-src">근거 레슨: ${sources}</p>` : ""}
      </div>`;
  }).join("");
  const level = state.hints.length;
  const more = level < 3
    ? `<button type="button" class="btn ghost small" id="sasu-next">다음 힌트 (${level + 1}/3)</button>`
    : `<p class="sasu-src">힌트는 여기까지예요. 그래도 막히면 해설 보기로 확인할 수 있어요.</p>`;
  const left = remaining != null ? `<p class="sasu-left">오늘 남은 힌트 ${esc(remaining)}회</p>` : "";
  openSasu(items + more + left);
  if (level < 3) $("#sasu-next").addEventListener("click", requestHint);
  const body = $("#sasu-body");
  body.scrollTop = body.scrollHeight;
}

async function requestHint() {
  if (state.hintBusy || state.hints.length >= 3) return;
  state.hintBusy = true;
  const q = currentQuestion();
  const level = state.hints.length + 1;
  openSasu(`<p class="sasu-msg muted">사수가 코드를 보고 있어요…</p>`);
  const res = await api(`questions/${q.id}/hints`, { progress_id: state.progressId, level });
  state.hintBusy = false;
  if (!res.ok) {
    // 한도 초과(429)가 아니면 다시 시도할 수 있게 버튼을 둔다
    const retry = res.status === 429 ? "" : `<button type="button" class="btn ghost small" id="sasu-next">다시 시도</button>`;
    openSasu(`<p class="sasu-msg">${esc(res.message)}</p>${retry}`);
    if (retry) $("#sasu-next").addEventListener("click", requestHint);
    return;
  }
  state.hints.push(res.data);
  renderHints(res.data.remaining_today);
}

function nextQuestion() {
  state.idx += 1;
  showQuestion();
}

function isIos() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent);
}
function isStandalone() {
  return window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
}

async function finishLesson() {
  if (state.busy) return;
  state.busy = true;
  const res = await api(`progress/${state.progressId}/complete`, {});
  state.busy = false;
  if (!res.ok) {
    showError(res.message);
    return;
  }
  const d = res.data;
  const loggedIn = document.body.dataset.loggedIn === "1";
  $("#quiz").hidden = true;

  const diag = state.lesson.is_diagnostic;
  let html = diag
    ? `<p class="eyebrow">사전 진단 완료</p>
    <h1>지금 실력을 쟀어요</h1>
    <div class="stats">
      <div><span class="label">맞힌 문제</span><strong>${esc(d.correct_count)} / ${esc(d.question_count)}</strong></div>
      <div><span class="label">걸린 시간</span><strong>${esc(minutesText(d.duration_sec))}</strong></div>
    </div>
    <p class="muted small">이 점수가 출발점이에요. 레슨을 여는 데는 영향이 없어요. 베타 마지막 주에 다른 문제로 한 번 더 재 볼게요.</p>`
    : `<p class="eyebrow">레슨 완료</p>
    <h1>오늘 몫을 끝냈어요</h1>
    <div class="stats">
      <div><span class="label">처음에 맞힌 문제</span><strong>${esc(d.correct_count)} / ${esc(d.question_count)}</strong></div>
      <div><span class="label">걸린 시간</span><strong>${esc(minutesText(d.duration_sec))}</strong></div>
    </div>`;

  if (d.week && !diag) { // 진단은 주간 목표(레슨 푼 날)에 들어가지 않는다
    html += `<div class="card week">${weekHtml(d.week)}</div>`;
  }
  if (!loggedIn) {
    // FR-01: 첫 레슨 결과 화면에서 가입 안내. 가입하면 이 기록이 계정에 붙는다
    html += `<div class="card">
        <h2>기록을 이어가려면 가입해 주세요</h2>
        <p class="muted small">방금 푼 레슨 기록이 계정에 저장되고, 다음 레슨부터 이어서 풀 수 있어요.</p>
        <a class="btn primary" href="/accounts/google/login/?next=/">구글로 계속하기</a>
        <a class="btn ghost" href="/accounts/signup/?next=/">이메일로 가입하기</a>
      </div>`;
  }
  if (isIos() && !isStandalone()) {
    // FR-13: 아이폰은 홈 화면에 추가해야 앱처럼 열리고 알림도 받을 수 있다
    html += `<div class="card tip"><p class="small">아이폰에서는 아래 공유 버튼 → <strong>홈 화면에 추가</strong>를 누르면 앱처럼 열 수 있어요.</p></div>`;
  }

  const result = $("#result");
  result.innerHTML = html;
  result.hidden = false;
  setActions([{ label: diag ? "오늘의 레슨 하러 가기" : "홈으로", kind: loggedIn ? "primary" : "ghost",
    onClick: () => (location.href = "/") }]);
  window.scrollTo(0, 0);
}

/* 구글 로그인 버튼: 링크(GET)로 가면 allauth가 '계속' 확인 화면을 한 번 더 보여 준다.
 * 누를 때 POST로 보내 확인 화면 없이 바로 구글 계정 선택으로 간다 (10/14 폰 점검).
 * CSRF 쿠키가 없으면(드문 경우) 원래 링크대로 확인 화면을 거친다 */
function postGoogleLogin(event) {
  const link = event.target.closest('a[href^="/accounts/google/login/"]');
  if (!link || !csrfToken()) return;
  event.preventDefault();
  const form = document.createElement("form");
  form.method = "post";
  form.action = link.getAttribute("href");
  const token = document.createElement("input");
  token.type = "hidden";
  token.name = "csrfmiddlewaretoken";
  token.value = csrfToken();
  form.appendChild(token);
  document.body.appendChild(form);
  form.submit();
}

/* ---------------- 시작 ---------------- */

document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  document.addEventListener("click", postGoogleLogin);
  if (page === "home") initHome();
  if (page === "records") initRecords();
  if (page === "lesson") {
    initLesson();
    $("#hint-btn").addEventListener("click", () => {
      if (state.hints.length) renderHints(null); else requestHint();
    });
    $("#sasu-close").addEventListener("click", (e) => { e.stopPropagation(); closeSasu(); });
    $("#sasu-toggle").addEventListener("click", (e) => {
      e.stopPropagation();
      setSasuMini(!$("#sasu").classList.contains("is-mini"));
    });
    $(".sasu-head").addEventListener("click", () => {
      if ($("#sasu").classList.contains("is-mini")) setSasuMini(false);
    });
    if ("ResizeObserver" in window) new ResizeObserver(updateSasuSpace).observe($("#sasu"));
    window.addEventListener("resize", updateSasuSpace);
    $("#q-code").addEventListener("scroll", markCodeScrollEnd, { passive: true });
    window.addEventListener("resize", fitCode);
    window.addEventListener("resize", fitSnippets);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { fitCode(); fitSnippets(); });
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
});
