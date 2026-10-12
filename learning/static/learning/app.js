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
  return String(md || "")
    .split(/\n{2,}/)
    .map((para) => `<p>${inline(para.trim())}</p>`)
    .join("");
}

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

/* API 호출. 실패해도 예외 대신 {ok:false, message}를 돌려줘 화면이 멈추지 않게 한다 */
async function api(path, body) {
  const options = { method: body === undefined ? "GET" : "POST", credentials: "same-origin", headers: {} };
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
  const { lesson, week, is_guest } = res.data;

  if (!lesson) {
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
    weekBox.innerHTML = weekHtml(week);
    weekBox.hidden = false;
  }
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
  intro.innerHTML = `${preview}<p class="tag">${esc(TRACK[l.track] || "")} · 약 ${esc(l.est_minutes)}분</p>
    <h1>${esc(l.title)}</h1>
    <div class="body">${markdown(l.body_md)}</div>`;
  setActions([{ label: `문제 풀기 (${l.questions.length}문제)`, onClick: startQuiz }]);
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
  $("#hint-btn").hidden = false;
  setActions([{ label: "제출하기", id: "submit-btn", onClick: submitAnswer, disabled: true }]);
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
}

function closeSasu() {
  $("#sasu").hidden = true;
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

  let html = `<p class="eyebrow">레슨 완료</p>
    <h1>오늘 몫을 끝냈어요</h1>
    <div class="stats">
      <div><span class="label">처음에 맞힌 문제</span><strong>${esc(d.correct_count)} / ${esc(d.question_count)}</strong></div>
      <div><span class="label">걸린 시간</span><strong>${esc(minutesText(d.duration_sec))}</strong></div>
    </div>`;

  if (d.week) {
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
  setActions([{ label: "홈으로", kind: loggedIn ? "primary" : "ghost", onClick: () => (location.href = "/") }]);
  window.scrollTo(0, 0);
}

/* ---------------- 시작 ---------------- */

document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  if (page === "home") initHome();
  if (page === "lesson") {
    initLesson();
    $("#hint-btn").addEventListener("click", () => {
      if (state.hints.length) renderHints(null); else requestHint();
    });
    $("#sasu-close").addEventListener("click", closeSasu);
    $("#q-code").addEventListener("scroll", markCodeScrollEnd, { passive: true });
    window.addEventListener("resize", fitCode);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(fitCode);
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
});
