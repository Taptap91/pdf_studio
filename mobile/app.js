/* PDF Studio · phone. Upload a PDF, tag it automatically, name it, download the tagged PDF and its settings file.
 * The tagging itself is engine.js, generated from pdf_studio.py by tools/build_mobile.py. Nothing leaves the phone. */
(() => {
  const $ = id => document.getElementById(id);
  const TAG_NAME = { toc: "Table of contents", h1: "Heading 1", h2: "Heading 2", h3: "Heading 3", h4: "Heading 4",
    para: "Paragraph", img: "Image", sec: "Section", grp: "Group" };
  const TAG_RGB = { toc: [0.44, 0.28, 0.87], h1: [0.79, 0.16, 0.16], h2: [0.76, 0.25, 0.05], h3: [0.18, 0.48, 0.23],
    para: [0.09, 0.39, 0.67], img: [0.63, 0.12, 0.30], sec: [0.53, 0.56, 0.59], grp: [0.04, 0.66, 0.47] };
  const GROUPS = ["Imperium", "Chaos", "Xenos"];

  let bytes = null, fileName = "", marks = null, tagging = false;
  const state = { title: "", game: "", edition: "", type: "Rules", section: "", group: "" };

  // The engine reads the file through pdf.js from a URL. On the phone the file is already in memory,
  // so this hands pdf.js the bytes directly.
  const pdfReady = import("./vendor/pdf.mjs").then(m => { m.GlobalWorkerOptions.workerSrc = "./vendor/pdf.worker.mjs"; return m; });
  window.loadPdfJs = async () => {
    const m = await pdfReady;
    // the engine also reads pdf.js's operator codes (OPS) from this object, so pass the whole module through
    return Object.assign({}, m, { getDocument: () => m.getDocument({ data: bytes.slice(0) }) });
  };

  const msg = (id, text, kind) => { const el = $(id); el.textContent = text; el.className = "msg" + (kind ? " " + kind : ""); };
  const show = (id, on) => $(id).classList.toggle("hidden", !on);

  // ---- step 1: choose ----
  $("pick").addEventListener("change", async e => {
    const f = e.target.files[0];
    if (!f) return;
    fileName = f.name;
    $("pickLabel").textContent = f.name;
    if (f.size > 40 * 1024 * 1024) msg("msg1", "That file is over 40 MB. It may be too big for this phone.", "err");
    else msg("msg1", "");
    bytes = new Uint8Array(await f.arrayBuffer());
    if (!state.title) { state.title = f.name.replace(/\.pdf$/i, ""); $("title").value = state.title; }
    show("step2", true); show("step3", true); show("step4", false);
    await tag();
  });

  // ---- step 2: tag ----
  async function tag() {
    if (!bytes || tagging) return;
    tagging = true;
    $("tagState").textContent = "Tagging the file…";
    $("tagCounts").textContent = "";
    $("retag").disabled = true;
    try {
      // Tag as 40K first, then tag again with the Kill Team profile if the file says it is a Kill Team.
      tagProfile = "40k";
      let result = await autoTagFile({ id: "phone", top1: 0, bottom1: 0 });
      if (result.meta && /kill\s*team/i.test(result.meta.game || "")) {
        tagProfile = "killteam";
        result = await autoTagFile({ id: "phone", top1: 0, bottom1: 0 });
      }
      marks = result;
      const by = {};
      result.forEach(m => { by[m.k] = (by[m.k] || 0) + 1; });
      const parts = Object.keys(by).sort().map(k => `${TAG_NAME[k] || k}: ${by[k]}`);
      $("tagState").textContent = result.length ? `Tagged ${result.length} items. Check the file on a computer if anything looks wrong.` : "Nothing was tagged. This file may be scanned images, or a layout the tagger doesn't know yet.";
      $("tagCounts").textContent = parts.join(" · ");
      if (result.meta && result.meta.game && !state.game) guess(result.meta);
    } catch (err) {
      marks = [];
      $("tagState").textContent = "Couldn't tag this file: " + (err.message || err);
    } finally {
      tagging = false;
      $("retag").disabled = false;
    }
  }
  $("retag").addEventListener("click", tag);

  // Fill in the name and game from the tagger's guess, but never over something the user typed.
  function guess(meta) {
    if (meta.edition && !$("edition").value.trim()) $("edition").value = meta.edition;
    if (meta.game) {
      const sel = $("game"), known = [...sel.options].map(o => o.value || o.text);
      if (known.includes(meta.game)) sel.value = meta.game;
      else { sel.value = "other"; $("gameOther").value = meta.game; }
      syncGame();
    }
    readState(); paintPath();
  }

  // ---- step 3: name ----
  const isKillTeam = () => /kill\s*team/i.test(state.game === "other" ? $("gameOther").value : state.game);
  function syncGame() { $("gameOther").classList.toggle("hidden", $("game").value !== "other"); }
  function readState() {
    state.title = $("title").value.trim();
    state.game = $("game").value === "other" ? $("gameOther").value.trim() : $("game").value;
    state.edition = $("edition").value.trim();
    state.type = $("type").value;
    state.section = $("section").value.trim();
    state.group = $("group").value;
  }
  function syncRows() {
    readState();
    show("sectionRow", state.type === "Sections");
    show("groupRow", isKillTeam() || state.type === "Factions" || state.type === "Sections");
  }
  function paintPath() {
    readState();
    const game = state.game || "<Game>", ed = state.edition || "<Edition>";
    const bucket = state.type;
    const name = state.title || "<Name>";
    $("path").textContent = `packs/incoming/${game}/${ed}/${bucket}/${name}.pdf`;
  }
  ["title", "game", "gameOther", "edition", "type", "section", "group"].forEach(id => {
    $(id).addEventListener("input", () => { syncGame(); syncRows(); paintPath(); show("step4", false); });
    $(id).addEventListener("change", () => { syncGame(); syncRows(); paintPath(); show("step4", false); });
  });
  $("game").addEventListener("change", syncGame);

  // Make the download. Ready when a name, game, edition and type are all given.
  function ready() {
    readState();
    if (!bytes) return "Choose a PDF first.";
    if (!state.title) return "Give it a name.";
    if (!state.game) return "Choose a game.";
    if (!state.edition) return "Give the edition, e.g. 3rd Edition.";
    if (state.type === "Sections" && !state.section) return "Give the section name, e.g. Kill Teams.";
    return "";
  }
  const toSave = document.createElement("button");
  toSave.id = "make"; toSave.type = "button"; toSave.textContent = "Make download";
  $("step3").appendChild(toSave);
  toSave.addEventListener("click", async () => {
    const problem = ready();
    if (problem) { msg("msg3", problem, "err"); return; }
    msg("msg3", "Making the file…");
    try {
      const pdf = await annotate(bytes, marks || []);
      const json = JSON.stringify(settings(), null, 2) + "\n";
      setLink("dlPdf", new Blob([pdf], { type: "application/pdf" }), `${safe(state.title)}.pdf`);
      setLink("dlJson", new Blob([json], { type: "application/json" }), `${safe(state.title)}.json`);
      show("step4", true);
      msg("msg3", "Ready. Download both files below.", "ok");
    } catch (err) {
      msg("msg3", "Couldn't make the file: " + (err.message || err), "err");
    }
  });

  function settings() {
    const s = { title: state.title };
    if (state.type === "Sections") s.section = state.section;
    if (state.group && GROUPS.includes(state.group)) s.group = state.group;
    return s;
  }
  const safe = t => t.replace(/[\\/:*?"<>|]+/g, "").trim() || "tagged";
  function setLink(id, blob, name) {
    const a = $(id);
    if (a.href.startsWith("blob:")) URL.revokeObjectURL(a.href);
    a.href = URL.createObjectURL(blob);
    a.download = name;
  }

  // Writes each tag into the PDF as an annotation, the same way the desktop app does.
  async function annotate(src, tags) {
    const { PDFDocument, PDFName, PDFNumber, PDFString, PDFArray } = PDFLib;
    const doc = await PDFDocument.load(src, { ignoreEncryption: true });
    const pages = doc.getPages();
    tags.forEach((t, i) => {
      const page = pages[t.p];
      if (!page) return;
      const box = page.getCropBox ? page.getCropBox() : page.getMediaBox();
      const L = box.x, B = box.y, W = box.width, H = box.height;
      const [r, g, b] = TAG_RGB[t.k] || [0.5, 0.5, 0.5];
      const x0 = L + t.x * W, top = B + H - t.y * H;
      const dict = doc.context.obj({});
      dict.set(PDFName.of("Type"), PDFName.of("Annot"));
      if (t.w != null) {
        const x1 = x0 + t.w * W, bot = top - t.h * H;
        dict.set(PDFName.of("Subtype"), PDFName.of("Square"));
        dict.set(PDFName.of("Rect"), PDFArray.withContext(doc.context));
        [x0, bot, x1, top].forEach(v => dict.get(PDFName.of("Rect")).push(PDFNumber.of(v)));
        const bs = doc.context.obj({ W: 4, S: "S" });
        dict.set(PDFName.of("BS"), bs);
      } else {
        dict.set(PDFName.of("Subtype"), PDFName.of("Text"));
        dict.set(PDFName.of("Name"), PDFName.of("Note"));
        dict.set(PDFName.of("Open"), PDFName.of("false"));
        dict.set(PDFName.of("Rect"), PDFArray.withContext(doc.context));
        [x0, top - 18, x0 + 18, top].forEach(v => dict.get(PDFName.of("Rect")).push(PDFNumber.of(v)));
      }
      const c = PDFArray.withContext(doc.context);
      [r, g, b].forEach(v => c.push(PDFNumber.of(v)));
      dict.set(PDFName.of("C"), c);
      dict.set(PDFName.of("F"), PDFNumber.of(4));
      const role = t.role ? `|role=${t.role}` : "";
      const link = t.link ? `|link=${t.link}` : "";
      dict.set(PDFName.of("Contents"), PDFString.of(`RF:${t.k}|${TAG_NAME[t.k] || t.k}${link}${role}`));
      dict.set(PDFName.of("T"), PDFString.of("RulesForge"));
      dict.set(PDFName.of("NM"), PDFString.of(String(t.id || i)));
      const ref = doc.context.register(dict);
      page.node.addAnnot(ref);
    });
    return await doc.save({ useObjectStreams: false });
  }

  show("step2", false); show("step3", false); show("step4", false);
  syncGame(); syncRows(); paintPath();
  ["title", "edition", "section"].forEach(id => $(id).addEventListener("input", paintPath));
})();
