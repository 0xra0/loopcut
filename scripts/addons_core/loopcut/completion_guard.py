"""Recognize imitations of Loopcut records, never ordinary claims about work; and a reply that
announces its next step but stops before taking it. Both are sent back from one budget a turn
(nudges_left), and neither when the reply says plainly that it stops or that nothing ran."""

import re


CORRECTION = "No tool ran this turn; nothing was created. Use the tools, or say plainly that nothing was done."
NOTICE = "This reply describes work, but no step ran, so nothing in your scene changed."
# Sent back after a reply that announced a step and called no tool (announces_step); never shown in the panel.
CONTINUE = "[Loopcut] Go ahead: do that now with your tools, or say plainly that you are stopping and why."
# Per user turn, and never two without a tool run in between. The turn's one record correction (CORRECTED:
# CORRECTION, or agent's copied-records retry), rig_gate's one bounce (RIG_CHECKED) and the one motion nudge
# (MOTION_CHECKED) spend from the same budget (nudges_left).
MAX_CONTINUES = 2
# Private scalar markers on the original user message: saved, restored and never sent to the model.
EXECUTED = "loopcut_executed_tools"
CORRECTED = "loopcut_unrun_corrected"
MOTION_CHECKED = "loopcut_motion_check_sent"  # agent._motion_claim sent the turn back once (motion_claims.nudge).
CONTINUED = "loopcut_step_continues"        # How many CONTINUE nudges this user turn has had.
CONTINUED_AT = "loopcut_step_continued_at"  # EXECUTED when the last one was sent.
RIG_CHECKED = "loopcut_rig_check_sent"      # rig_gate sent the turn back once on its own rig check's problem.

_ACTIONS = (r"create|make|build|add|model|move|rotate|scale|resize|delete|remove|duplicate|rename|"
            r"parent|bevel|extrude|assign|apply|change|edit|fix|repair|replace|position|align|"
            r"place|set|color|colour|texture|sculpt|animate|light")
_REQUEST = re.compile(
    r"^(?:(?:please|now|then|also)\s+)*(?:(?:can|could|would|will)\s+you\s+|"
    r"i\s+(?:want|need|would like)\s+you\s+to\s+)?(?:please\s+)?(?:" + _ACTIONS + r")\b", re.I)
_INFORMATION = re.compile(r"^(?:how|what|why|when|where|explain|describe|tell me|plan|write)\b", re.I)
_DELIVERABLE = re.compile(r"\s+(?:[\w-]+\s+){0,3}(?:plan|script|code|example|tutorial|guide|list)\b", re.I)
_UI_RECORD = re.compile(
    r"^(?:(?:Verified by Loopcut on this scene|(?:Placement measured|Rig checked|Checked again) this turn):"
    r"\s*\d+ of \d+ checks\.?|"
    r"Scene changes(?:\s*[:(].*)?|"
    r"\d+ (?:added|removed|changed)(?:,\s*\d+ (?:added|removed|changed))*\.?|"
    r"\d+ added\s*/\s*(?:\d+\s+)?changed(?:\s*/\s*\d+ removed)?\.?)$", re.I)
_ILLUSTRATION = re.compile(
    r"\b(?:example|sample|previous (?:step|turn)|earlier (?:step|turn|output|result|record))\b|"
    r"^plan\b|^(?:I'll|I will)\b.*:$|\b(?:here|following)\b.*\b(?:code|script)\b", re.I)
_RESULT = r"(?:ok|done|success(?:ful)?|failed|printed|output|result)\b"
# _ILLUSTRATION's explicit part, plurals included. Not in _ILLUSTRATION itself: OLD_RECORD_HEAD begins "Earlier steps.",
# and the 0.2.13 correction must still see the forged records after a copied head (rel015-app).
_EXAMPLE_INTRO = re.compile(r"\b(?:example|sample|previous (?:steps?|turns?)|"
                            r"earlier (?:steps?|turns?|outputs?|results?|records?))\b", re.I)


def scene_request(message: dict) -> bool | None:
    """Resolve informational intent before action clauses, ignoring automatic scene context.

    None leaves an implicit request ("a prop") to the router, if available.
    """
    content = message.get("content", "")
    if isinstance(content, list):
        content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    if not isinstance(content, str):
        return False
    text = content.split("</scene_context>", 1)[-1].split("<routing>", 1)[0].strip()
    if _INFORMATION.match(text):
        return False
    parts = [part.strip() for part in re.split(r"[\n.!?]+", text)]
    requests = [(part, _REQUEST.match(part)) for part in parts]
    if any(match and _DELIVERABLE.match(part[match.end():]) for part, match in requests):
        return False
    if any(match for _, match in requests):
        return True
    return False if any(_INFORMATION.match(part) for part in parts) else None


def _plain(line: str) -> str:
    return re.sub(r"^(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)", "", line.strip()).replace("**", "").replace("`", "")


def introduces_example(line: str) -> bool:
    """A line that introduces an example or an account of earlier steps ("Example:", "In the previous steps:"),
    after which context.copied_records leaves record-shaped text alone. Narrower than imitates_records' reading
    (_ILLUSTRATION): an "I'll ...:" or a question before forged records does not make them an example."""
    return bool(_EXAMPLE_INTRO.search(_plain(line.replace("’", "'"))))


def imitates_records(text: str, tool_names) -> bool:
    """Only record-shaped text using this session's tools or the runtime's UI labels.

    Prose, quoted records and ordinary code fences are not execution records. Explicit examples,
    plans and historical explanations also leave their following block alone.
    """
    names = "|".join(re.escape(name) for name in tool_names) or r"(?!)"
    tool = re.compile(r"^(?:" + names + r")(?:\s*\(.*\)(?:\s*:\s*" + _RESULT + r".*)?|"
                      r"\s*:\s*(?:" + _RESULT + r"|\{).*|"
                      r'\s+["\u201c].+?["\u201d]\s*:\s*' + _RESULT + r".*|\s+" + _RESULT + r"[.!]?)$", re.I)
    label = re.compile(r"^(?:" + names + r")\s*:?$", re.I)
    json_name = re.compile(r'^\{?\s*"(?:name|tool)"\s*:\s*"(?:' + names + r')"\s*[,}]', re.I)
    payload = re.compile(r'"(?:arguments|parameters|code|status|output|result)"\s*:', re.I)
    following = re.compile(r"^(?:```|~~~|" + _RESULT + r"|import\s|from\s|bpy\.|\{)", re.I)
    lines = text.replace("’", "'").splitlines()
    fence, example, example_body = "", False, False
    code_fence = False
    for index, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith(">"):
            continue
        if line.startswith(("```", "~~~")):
            if fence:
                if line.startswith(fence):
                    fence, code_fence = "", False
                    example_body = True
            else:
                fence = line[:3]
                language = line[3:].strip()
                if not example and label.fullmatch(language):
                    return True
                code_fence = language.lower() not in ("", "json", "text", "plaintext")
            continue
        if not line:
            if example_body and not fence:
                example, example_body = False, False
            continue
        line = _plain(line)
        if not fence and (_ILLUSTRATION.search(line) or line.endswith("?")):
            example, example_body = True, False
            continue
        if example:
            example_body = True
            continue
        if code_fence:
            continue
        if _UI_RECORD.fullmatch(line) or tool.fullmatch(line):
            return True
        if label.fullmatch(line):
            next_line = next((s for s in lines[index + 1:] if s.strip()), "")
            if following.match(next_line.strip()) or next_line.startswith(("    ", "\t")):
                return True
        if json_name.match(line) and payload.search("\n".join([line, *lines[index + 1:index + 4]])):
            return True
    return False


# --- Announced steps -------------------------------------------------------------------------------------
# "I will next import ...", "I'll now search ...", "Let's try ...": first person, about to act, with a verb
# for something the tools do. Each language needs an immediate, first-person action; questions
# and work deferred to the user must still end the turn.
_STEP_VERBS = (r"import|search|look|browse|find|fetch|download|grab|bring|load|add|create|make|build|model|place|"
               r"put|position|move|rotate|scale|resize|arrange|align|set|adjust|apply|assign|tweak|refine|fix|"
               r"repair|replace|swap|remove|delete|duplicate|generate|render|capture|check|inspect|verify|test|try|"
               r"retry|attempt|start|begin|continue|proceed|work|focus|populate|furnish|decorate|light|texture|"
               r"sculpt|animate|rig|run|organi[sz]e|parent|join|combine|assemble|scatter|configure|update|change|"
               r"edit|improve|conduct|perform|measure")
_LEAD = (r"(?:(?:now|next|then|first|also|just|quickly|immediately|again|instead|finally|go\s+ahead\s+and|"
         r"go\s+on\s+to|move\s+on\s+to|proceed\s+to|need\s+to|have\s+to|try\s+to|start\s+by|begin\s+by|"
         r"continue\s+by)[\s,]+){0,3}")
_INTENT = re.compile(r"\b(?:i\s+will|i'll|i\s+shall|i(?:'m|\s+am)\s+(?:now\s+)?(?:going|about)\s+to|let\s+me|"
                     r"let's|let\s+us)\s+" + _LEAD + r"(?:" + _STEP_VERBS + r")\b", re.I)
# The step waits on the user, a condition or a later turn: not something to do now. Any "you" in the
# sentence ("the one you pick"), any condition ("If this looks good, I'll..."), and a step that follows
# something else ("Say the word and I'll...", "Sign in..., then I'll...") all count as waiting.
_DEFERRED = re.compile(
    r"\byou(?:r|'re|'d)?\b|\b(?:if|once|assuming|provided|whichever)\b|\b(?:and|then),?\s+(?:i'll|i\s+will)\b|"
    r"\b(?:later|next\s+time|another\s+time|tomorrow)\b|"
    r"\bin\s+(?:the|a)\s+(?:next|future|later|follow-?up)\s+(?:turn|message|reply|response|session|conversation|request)\b",
    re.I)
# Anywhere in the reply: a question or choice for the user, a hand-off, or a report that the work is done.
_HANDOFF = re.compile(
    r"\?|"
    r"\b(?:let\s+me\s+know|feel\s+free|would\s+you\s+(?:like|prefer|rather)|do\s+you\s+want|want\s+me\s+to|"
    r"shall\s+i|should\s+i|your\s+call|up\s+to\s+you|"
    r"if\s+you(?:'d|\s+would)?\s+(?:like|want|prefer|rather)|"
    r"you\s+(?:can|could|might|may)\s+(?:now|also|then|want|wish|choose|pick|tell|let)|"
    r"your\s+(?:approval|confirmation|go-?ahead|ok|okay|input|feedback|preference|choice)|"
    r"i(?:'m|\s+am)\s+(?:stopping|done|finished)|i(?:'ll|\s+will)\s+(?:stop|pause|wait|end\s+here|leave\s+it)|"
    r"stopping\s+here|(?:that|this)(?:'s|\s+is)\s+(?:all|it)|all\s+(?:done|set)|"
    r"(?:is|are)\s+(?:now\s+)?(?:done|complete|completed|finished|ready))\b|"
    r"(?:^|[.!:]\s+)(?:please\s+)?(?:done|finished|choose|pick|select|confirm|tell\s+me|reply)\b",
    re.I | re.M)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

# Inflected first-person verbs, or an explicit future cue paired with a tool action.
_LOCAL_INTENTS = tuple(re.compile(pattern, re.I) for pattern in (
    r"^(?:(?:сейчас|теперь|затем|далее|сначала|после этого)[,\s]+)?(?:я\s+)?"
    r"(?:добавлю|создам|проверю|найду|импортирую|загружу|построю|изменю|исправлю|"
    r"применю|настрою|перемещу|поверну|удалю|продолжу|сделаю)\b",
    r"^(?:(?:次に|まず|これから|続いて|今から|では)[、\s]*)?.*?"
    r"(?:追加|作成|確認|配置|検索|読み込み|調整|修正|適用|移動|削除|生成|レンダリング)"
    r"(?:します|していきます|しましょう)(?:[。！!]|$)",
    r"\b(?:voy\s+a\s+(?:crear|añadir|agregar|buscar|importar|comprobar|revisar|ajustar|"
    r"aplicar|mover|eliminar|generar|renderizar)|"
    r"crearé|añadiré|agregaré|buscaré|importaré|comprobaré|revisaré|ajustaré|aplicaré|moveré|"
    r"eliminaré|generaré|renderizaré)\b",
    r"\b(?:سأضيف|سأنشئ|سأفحص|سأبحث|سأستورد|سأضبط|سأطبق|سأحرك|سأحذف|سأولد)\b",
    r"\b(?:nu\s+|derefter\s+|først\s+)?(?:vil\s+jeg|jeg\s+vil)\s+(?:nu\s+)?"
    r"(?:tilføje|oprette|bygge|importere|søge|kontrollere|justere|anvende|flytte|slette|generere|rendere)\b",
))
# Each wait pattern belongs to the intent pattern at the same index. A material name or
# an earlier completed step must not turn an immediate action into a hand-off.
_LOCAL_WAITS = tuple(re.compile(pattern, re.I) for pattern in (
    r"\b(?:если|когда|выберите|подтвердите|скажите|жду|завтра|позже|в следующий раз|дайте знать|"
    r"какой|какую|какие|хотите|вашего подтверждения|по запросу|при необходимости|по желанию|"
    r"на (?:ваш|твой) вкус|как вам нравится|по мере необходимости)\b",
    r"(?:ください|必要なら|必要に応じて|お好み|好みに合わせて|れば|たら|であれば|ご希望|場合|選んで|教えて|お知らせ|待ちます|明日|後で|次回|ですか|ますか)",
    r"\b(?:si|cuando|elige|dime|confirma|avísame|quieres|prefieres|prefieras|mañana|más tarde|"
    r"próximo mensaje|lo que\s+(?:quieras|prefieras|necesites|elijas)|a (?:tu|su) gusto|según (?:lo )?necesites)\b",
    r"\b(?:إذا|إن أردت|عندما|اختر|أخبرني|أكد|تأكيدك|هل|ما اللون|غدا|لاحقا|سأنتظر|لن|عند طلبك|كما تريد|حسب (?:رغبتك|الحاجة|ذوقك))\b",
    r"\b(?:hvis|når|vælg|bekræft|fortæl|sig til|vil du|skal jeg|hvilken|hvilket|senere|i morgen|efter dine ønsker|"
    r"efter behov|som du ønsker)\b",
))
_LOCAL_FINISHED = tuple(re.compile(pattern, re.I) for pattern in (
    r"^готово\b", r"^(?:完了しました|終わりました)", r"^(?:listo|terminado)\b",
    r"^انتهيت\b", r"^færdig\b",
))
# An explicit finished deliverable ends the reply, as English's "is ready" does. A
# completed-step prefix ("Done: ... Next ...") can still introduce immediate work.
_LOCAL_READY = tuple(re.compile(pattern, re.I) for pattern in (
    r"^(?:\w+\s+){1,4}(?:готов|сделан)[аоы]?\s*[.!]?$",
    r"^.{1,40}[はが](?:完成しました|準備ができました|できました)[。！!]?$",
    r"^(?:(?:el|la|los|las)\s+)?(?:\w+\s+){1,4}(?:est[áa]n?\s+)?list[oa]s?\s*[.!]?$",
    r"^(?:\w+\s+){1,4}جاهز[ة]?\s*[.!]?$",
    r"^(?:\w+\s+){1,4}er\s+(?:klar|færdig)\s*[.!]?$",
))


def nudges_left(request: dict) -> int:
    """What is left of the user turn's MAX_CONTINUES: the announced-step nudges sent (CONTINUED), the one
    record correction (CORRECTED), rig_gate's one bounce (RIG_CHECKED) and the one motion nudge (MOTION_CHECKED),
    counted on the user's message so a resumed turn keeps the count."""
    return (MAX_CONTINUES - request.get(CONTINUED, 0) - bool(request.get(CORRECTED))
            - bool(request.get(RIG_CHECKED)) - bool(request.get(MOTION_CHECKED)))


# The whole reply stopping, or nothing having run: "I'm stopping here.", "I'll stop now, since...", "Nothing ran.",
# "No tools were run this turn." Each ends there, at a clause: "I'll stop the animation at frame 120" and "nothing
# ran into the chest" are work, and "nothing was changed on the legs" is part of a claim. This exit turns the
# corrections off, so a false yes leaves a false account standing.
_WHEN = r"(?:\s+(?:here|now|there|yet|at\s+all|for\s+now|this\s+turn|in\s+this\s+(?:reply|turn)))?"
_END = r"(?=\s*(?:[.!;:,\u2014\u2013-]|$)|\s+(?:because|since|as|until|and|but|so)\b)"
_STOPPING = re.compile(r"\b(?:i(?:'m|\s+am)\s+stopping|i(?:'ll|\s+will)\s+stop|stopping\s+here|"
                       r"nothing\s+(?:ran|has\s+run|was\s+run)|no\s+(?:tools?|steps?|calls?)\s+(?:ran|were\s+run))"
                       + _WHEN + _END, re.I | re.M)


def stops_honestly(text) -> bool:
    """A reply that says plainly it is stopping or that nothing ran: the answer every nudge offers, so
    agent.run_loop sends it back for neither an announced step nor a record correction."""
    return isinstance(text, str) and bool(_STOPPING.search(text.replace("’", "'")))


def announces_step(text: str) -> bool:
    """Whether a reply with no tool calls ends by announcing work it is about to do now ("I will next import
    the desk.", "Let's try a broader search."), so the turn should go on instead of ending.

    Only the last two sentences of the last paragraph count, and only a first-person step with a tool-shaped
    verb, in a sentence with no "you", no condition ("if", "once"...), no "and/then I'll" after something the
    user does, and no later turn; nor one that introduces something (ends in ":").
    Never when the reply asks anything ("?" anywhere), offers a choice, hands off ("let me know if..."),
    or says the work is done or stopping; nor when it ends on a list (a plan laid out, not a step begun).
    """
    if not isinstance(text, str):
        return False
    prose = re.sub(r"(?:```|~~~).*?(?:```|~~~|\Z)", "\n\n", text.replace("’", "'"), flags=re.S)
    if re.search(r"[?¿？؟]", prose):
        return False
    paragraphs = [p for p in re.split(r"\n\s*\n", prose) if p.strip()]
    if not paragraphs:
        return False
    lines = [line for line in paragraphs[-1].splitlines() if line.strip()]
    if _LIST_ITEM.match(lines[-1]):
        return False
    sentences = [s.strip() for line in lines for s in re.split(r"(?<=[.!])\s+|(?<=[。！？؟])\s*", _plain(line))
                 if s.strip()]
    tail = " ".join(sentences[-2:])
    first = re.split(r"(?<=[.!])\s+|(?<=[。！？؟])\s*", _plain(paragraphs[0]))[0].strip()
    if any(ready.search(first) for ready in _LOCAL_READY):
        return False
    for sentence in sentences[-2:]:
        if sentence.rstrip(" .!*_").endswith((":", "：")):
            continue
        if _INTENT.search(sentence) and not _DEFERRED.search(sentence) and not _HANDOFF.search(prose):
            return True
        if any(intent.search(sentence) and not wait.search(tail)
               and not (finished.search(sentences[-1]) and not intent.search(sentences[-1]))
               for intent, wait, finished in zip(_LOCAL_INTENTS, _LOCAL_WAITS, _LOCAL_FINISHED)):
            return True
    return False


# The rest of a plan the model stated after its 3D step (agent._finish_plan: a Turn it into 3D click ran that step).
# A priced step reads "(6 credits)", "(about 5 credits)", "(~80 credits, $0.80)".
_PRICED = re.compile(r"\((?:about|around|roughly|~)?\s*\d[\d,]*(?:\.\d+)?\s+credits?\b[^)]*\)", re.I)
_TO_3D = re.compile(r"\b(?:3[ -]?d|three-dimensional)\b", re.I)
_PLAN_VERBS = (_STEP_VERBS + r"|optimi[sz]e|retopologi[sz]e|clean|decimate|reduce|simplify|export|prepare|convert|"
               r"bake|paint")
# In the 3D step's own sentence: what follows it is a step when it is priced, sequenced or "and <does something>".
_THEN = re.compile(r"\b(?:then|after\s+that|afterwards|next|finally|lastly)\b", re.I)
_AND_DOES = re.compile(r"^\W*(?:and|plus)\s+(?:(?:i'll|i\s+will)\s+)?(?:also\s+)?(?:" + _PLAN_VERBS + r")\b", re.I)
# A later sentence is a step of the same plan when it is a list item, priced, or leads with a sequence word.
_LEADS = re.compile(r"^\W*(?:(?:and\s+)?then|after\s+that|afterwards|next|finally|lastly)\b|"
                    r"^\W*i(?:'ll|\s+will)\s+then\b", re.I)
_OWN_PRICE_GAP = re.compile(r"[,;:]|\b(?:then|and|after|next|before|or)\b", re.I)
MAX_PLAN_REST = 300


def _plan_units(text: str) -> list:
    """(sentence, is a list item) in order; None between paragraphs, where a plan's steps stop."""
    units = []
    for line in text.replace("’", "'").replace("**", "").replace("`", "").splitlines():
        if not line.strip():
            units.append(None)
            continue
        item = bool(_LIST_ITEM.match(line))
        for k, sentence in enumerate(s for s in re.split(r"(?<=[.!?])\s+", _plain(line)) if s.strip()):
            units.append((sentence, item and k == 0))
    return units


def _after_3d_step(sentence: str, credits) -> int | None:
    """Where the 3D step ends in this sentence, its own price included: the first 3D mention priced right after it
    ("a textured 3D model (80 credits)"), else the last 3D mention, else the price the click was charged."""
    last = None
    for found in _TO_3D.finditer(sentence):
        own = _PRICED.search(sentence, found.end())
        if own and not _OWN_PRICE_GAP.search(sentence[found.end():own.start()]):
            return own.end()
        last = found.end()
    if last is not None:
        return last
    for price in _PRICED.finditer(sentence) if type(credits) in (int, float) and credits > 0 else ():
        if re.search(rf"(?<![\d.,]){credits:g}(?![\d.,]*\d)", price.group().replace(",", "")):
            return price.end()
    return None


def plan_after(replies, credits=None) -> str:
    """The rest of the plan a turn's replies stated after its 3D step, in the model's words ("and optimize it for
    game use (about 5 credits)."), or "" when none states a step after it. The 3D step is found as _after_3d_step
    says; its rest is what follows it in that sentence and the sentences or list items right after it that are
    steps themselves. It is a step when priced, sequenced (then, next, after that, finally) or "and" with what it
    does; anything else after the 3D step ("so you can approve the design") is not."""
    for text in replies:
        if not isinstance(text, str):
            continue
        units = _plan_units(text)
        for i, unit in enumerate(units):
            if unit is None:
                continue
            end = _after_3d_step(unit[0], credits)
            if end is None:
                continue
            head = unit[0][end:].strip()
            rest = [head] if (_PRICED.search(head) or _THEN.search(head) or _AND_DOES.match(head)) else []
            for later in units[i + 1:]:
                if later is None or not (later[1] or _PRICED.search(later[0]) or _LEADS.match(later[0])):
                    break
                rest.append(later[0])
            rest = " ".join(part.strip(" ,;:") for part in rest if part.strip(" ,;:.!?"))
            if rest:
                return rest[:MAX_PLAN_REST]
    return ""
