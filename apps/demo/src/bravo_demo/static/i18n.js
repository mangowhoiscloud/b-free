// Plain-language strings for beginners. Korean is the default, Japanese is the toggle.
"use strict";
window.I18N = {
  ko: {
    title: "금융 상담실", subtitle: "상담사와 실시간으로 이야기해요",
    send: "보내기", attach: "파일 첨부", placeholder: "메시지를 입력하세요",
    welcome: "어서 오세요", askName: "채팅에 보일 이름을 적어 주세요.", namePlaceholder: "예: 김고객", start: "시작하기",
    servedBy: "지금 이 화면을 보내는 곳", envPrefix: "연결된 곳",
    live: "live", connecting: "연결 중", reconnecting: "다시 연결 중", waiting: "잠시 대기",
    sending: "보내는 중", sent: "보냄",
    retry: "다시 보내기", delayed: "전송이 늦어지고 있어요. 잠시 후 '다시 보내기'를 눌러 주세요.",
    failed: "보내지 못했어요.", invalid: "보낼 수 없는 내용이에요.",
    attachPaused: "첨부 일시 중지: 지금은 파일을 올릴 수 없어요. 잠시 후 다시 시도해 주세요.",
    attachTooBig: "파일이 너무 커요. {max} 이하로 올려 주세요.",
    attachFailed: "파일을 올리지 못했어요. 다시 시도해 주세요.",
    attachUploading: "파일을 올리는 중…", attachOpen: "열기", attachDeleted: "삭제된 첨부",
    attachRetry: "첨부 다시 시도",
    attachMissing: "첨부를 찾을 수 없어요. 다시 올려 주세요.",
    envChanged: "이제 {env}에서 서비스하고 있어요. 대화는 그대로 이어져요.",
    reconnected: "다시 연결됐어요.",
    writesPaused: "서비스를 옮기는 중이라 잠시 보내기를 멈췄어요. 곧 자동으로 다시 보낼게요.",
    dbWaiting: "서버가 잠시 쓰기를 멈췄어요. 보낸 메시지는 곧 이어서 전달돼요.",
    empty: "아직 메시지가 없어요. 첫 인사를 남겨 보세요.",
    joinTitle: "휴대폰으로 상담실에 들어오세요", joinHelp: "카메라로 QR 코드를 비추면 채팅방이 열려요.",
    me: "나",
  },
  ja: {
    title: "金融相談室", subtitle: "担当者とリアルタイムで話せます",
    send: "送信", attach: "ファイル添付", placeholder: "メッセージを入力してください",
    welcome: "ようこそ", askName: "チャットに表示する名前を入力してください。", namePlaceholder: "例: 山田", start: "はじめる",
    servedBy: "この画面を配信している場所", envPrefix: "接続先",
    live: "live", connecting: "接続中", reconnecting: "再接続中", waiting: "少々お待ちください",
    sending: "送信中", sent: "送信済み",
    retry: "再送信", delayed: "送信に時間がかかっています。少し待ってから「再送信」を押してください。",
    failed: "送信できませんでした。", invalid: "送信できない内容です。",
    attachPaused: "添付一時停止: 今はファイルをアップロードできません。しばらくしてからお試しください。",
    attachTooBig: "ファイルが大きすぎます。{max} 以下にしてください。",
    attachFailed: "ファイルをアップロードできませんでした。もう一度お試しください。",
    attachUploading: "ファイルをアップロード中…", attachOpen: "開く", attachDeleted: "削除された添付",
    attachRetry: "添付を再試行",
    attachMissing: "添付が見つかりません。もう一度アップロードしてください。",
    envChanged: "現在は{env}から配信しています。会話はそのまま続きます。",
    reconnected: "再接続しました。",
    writesPaused: "サービスを移行中のため送信を一時停止しています。まもなく自動で再送します。",
    dbWaiting: "サーバーが一時的に書き込みを止めています。送ったメッセージはまもなく届きます。",
    empty: "まだメッセージはありません。最初のあいさつをどうぞ。",
    joinTitle: "スマートフォンで相談室に参加", joinHelp: "カメラでQRコードを読み取るとチャットが開きます。",
    me: "自分",
  },
};
window.i18nLang = (function () {
  try { const v = localStorage.getItem("bravo.lang"); if (v === "ja" || v === "ko") return v; } catch (e) { /* storage blocked */ }
  return (navigator.language || "").toLowerCase().startsWith("ja") ? "ja" : "ko";
})();
window.t = function (key, vars) {
  const dict = window.I18N[window.i18nLang] || window.I18N.ko;
  let s = dict[key] != null ? dict[key] : (window.I18N.ko[key] || key);
  if (vars) for (const k of Object.keys(vars)) s = s.replace("{" + k + "}", vars[k]);
  return s;
};
window.applyI18n = function () {
  document.documentElement.lang = window.i18nLang;
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = window.t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = window.t(el.dataset.i18nPlaceholder); });
  document.title = window.t(document.body.classList.contains("join") ? "joinTitle" : "title");
  const btn = document.getElementById("lang");
  if (btn) btn.textContent = window.i18nLang === "ko" ? "日本語" : "한국어";
};
window.toggleLang = function () {
  window.i18nLang = window.i18nLang === "ko" ? "ja" : "ko";
  try { localStorage.setItem("bravo.lang", window.i18nLang); } catch (e) { /* storage blocked */ }
  window.applyI18n();
};
