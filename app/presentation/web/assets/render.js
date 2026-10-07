// All user, model and server text reaches the DOM through textContent.
// Clinical text is preserved verbatim; HTML/Markdown is never executed.
export function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

export function button(label, action, className) {
  const element = node("button", label, className);
  element.type = "button";
  element.addEventListener("click", action);
  return element;
}

export function fields(values) {
  const list = node("dl");
  for (const [label, value] of Object.entries(values)) {
    if (value != null) list.append(node("dt", label), node("dd", value));
  }
  return list;
}

export function citations(items) {
  const root = node("section");
  if (!items?.length) return root;
  root.setAttribute("aria-label", "Citations");
  root.append(node("h3", "Evidence & citations"));
  items.forEach((item, index) => {
    const detail = node("details", undefined, "citation");
    detail.append(node("summary", `[${index + 1}] ${item.document_name} · ${item.section || "Document"}${item.page == null ? "" : ` · page ${item.page}`}`));
    detail.append(node("blockquote", item.text_snippet));
    detail.append(fields({ "Document ID": item.document_id, "Chunk ID": item.chunk_id }));
    root.append(detail);
  });
  return root;
}

export function message(role, content, answer, onTrace) {
  const root = node("article", undefined, `message ${role}${answer?.refused ? " refusal" : ""}`);
  root.append(node("h3", role === "user" ? "You" : answer?.refused ? "Refusal · insufficient evidence" : "Answer"));
  root.append(node("div", answer?.answer ?? content, "prose"));
  if (answer) {
    root.append(citations(answer.citations));
    if (answer.trace_id) root.append(button("View answer trace", () => onTrace(answer.trace_id)));
  }
  return root;
}

export function renderDiff(target, diff) {
  target.replaceChildren();
  if (!diff.length) { target.textContent = "No changes."; return; }
  for (const line of diff) {
    const sign = line.kind === "add" ? "+ " : line.kind === "remove" ? "− " : "  ";
    const text = line.text.replace(/\n$/, "").replace(/\r/g, "␍");
    target.append(node("span", sign + text + (line.text.endsWith("\n") ? "" : " [no final newline]"), `diff-line diff-${line.kind}`));
  }
}
