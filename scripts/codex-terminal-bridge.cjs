var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));

// src/codexTerminalBridgeCli.ts
var import_child_process2 = require("child_process");
var import_path4 = require("path");
var import_fs5 = require("fs");
var import_crypto = require("crypto");

// src/platform.ts
var os = __toESM(require("os"), 1);
var path = __toESM(require("path"), 1);
var import_fs = require("fs");
var isMac = process.platform === "darwin";
var isWin = process.platform === "win32";
var isLinux = process.platform === "linux";
var homeDir = os.homedir();
var pathDelim = path.delimiter;
var skaileBrainDir = process.env.AGENTIC_OS_BRAIN_DIR ?? path.join(homeDir, "Documents", "skaile-brain");
var ALWAYS_ALLOWED_DIRS = process.env.AGENTIC_OS_DATA_DIR !== void 0 ? process.env.AGENTIC_OS_BRAIN_DIR ? [process.env.AGENTIC_OS_BRAIN_DIR] : [] : [
  skaileBrainDir,
  path.join(homeDir, "Documents", "Projects", "katchup-lead-automation")
];
function candidateBinDirs() {
  if (isWin) {
    const appData = process.env.APPDATA ?? path.join(homeDir, "AppData", "Roaming");
    const local = process.env.LOCALAPPDATA ?? path.join(homeDir, "AppData", "Local");
    return [
      path.join(appData, "npm"),
      // npm global (.cmd shims)
      path.join(local, "Microsoft", "WindowsApps"),
      // winget/Store
      path.join(homeDir, ".bun", "bin"),
      path.join(homeDir, ".local", "bin")
    ];
  }
  return [
    path.join(homeDir, ".npm-global", "bin"),
    path.join(homeDir, ".bun", "bin"),
    path.join(homeDir, ".local", "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin"
  ];
}
function buildPath() {
  const existing = process.env.PATH ?? process.env.Path ?? "";
  return [...candidateBinDirs(), existing].filter((p) => p.length > 0).join(pathDelim);
}
function spawnEnv(extra, provider = "claude") {
  const base = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (typeof v === "string") base[k] = v;
  }
  base.PATH = buildPath();
  if (provider === "claude") {
    base.CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN = "1";
    base.CLAUDE_CODE_DISABLE_MOUSE = "1";
  } else {
    delete base.CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN;
    delete base.CLAUDE_CODE_DISABLE_MOUSE;
    delete base.CLAUDECODE;
    delete base.CLAUDE_CODE_ENTRYPOINT;
  }
  base.TERM = "xterm-256color";
  if (extra !== void 0) {
    for (const [k, v] of Object.entries(extra)) base[k] = v;
  }
  return base;
}
function resolveBinary(name) {
  const exts = isWin ? ["", ".cmd", ".exe", ".bat"] : [""];
  const dirs = [
    ...candidateBinDirs(),
    ...(process.env.PATH ?? "").split(pathDelim)
  ];
  for (const dir of dirs) {
    if (dir.length === 0) continue;
    for (const ext of exts) {
      const full = path.join(dir, name + ext);
      try {
        if ((0, import_fs.existsSync)(full)) return full;
      } catch (_) {
      }
    }
  }
  return void 0;
}
function codexBinary() {
  return process.env.CODEX_BIN ?? resolveBinary("codex") ?? (isWin ? "codex.cmd" : "codex");
}

// src/codexBridge.ts
var import_timers = require("timers");
var import_child_process = require("child_process");
var import_fs4 = require("fs");
var import_net = require("net");
var import_path3 = require("path");

// src/sessionIdentity.ts
function object(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}
function nativeThreadId(value) {
  return typeof value === "string" && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value);
}
var SessionIdentityObserver = class {
  constructor(tabId, launchId, change) {
    this.change = change;
    this.pending = /* @__PURE__ */ new Map();
    this.sequence = 0;
    this.selected = 0;
    this.disabled = false;
    this.metadata = {
      version: 1,
      provider: "codex",
      tabId,
      launchId,
      status: "starting",
      identityStatus: "pending",
      updatedAt: (/* @__PURE__ */ new Date()).toISOString()
    };
  }
  get current() {
    return { ...this.metadata };
  }
  emit(patch) {
    this.metadata = {
      ...this.metadata,
      ...patch,
      updatedAt: (/* @__PURE__ */ new Date()).toISOString()
    };
    this.change(this.current);
  }
  unavailable(reason) {
    this.disabled = true;
    this.pending.clear();
    this.emit({
      identityStatus: "unavailable",
      nativeThreadId: void 0,
      nativeSessionId: void 0,
      // The TUI continues after an observer failure and may accept unseen turns.
      // Never preserve an old "empty thread" assumption for future recovery.
      hasUserTurn: void 0,
      error: reason
    });
  }
  request(v) {
    if (this.disabled || !object(v) || !(typeof v.id === "string" || typeof v.id === "number") || typeof v.method !== "string")
      return;
    if (["thread/start", "thread/resume", "thread/fork"].includes(v.method)) {
      if (this.pending.size > 100) {
        this.unavailable("Zu viele offene native Sessionwechsel");
        return;
      }
      this.pending.set(String(v.id), {
        method: v.method,
        sequence: ++this.sequence
      });
      this.selected = this.sequence;
    }
  }
  response(v) {
    if (this.disabled || !object(v)) return;
    if (typeof v.id === "string" || typeof v.id === "number") {
      const request = this.pending.get(String(v.id));
      if (request) {
        this.pending.delete(String(v.id));
        if (request.sequence !== this.selected) return;
        if (v.error !== void 0) {
          this.emit({ error: "Nativer Sessionwechsel fehlgeschlagen" });
          return;
        }
        const result = v.result;
        if (!object(result) || !object(result.thread) || !nativeThreadId(result.thread.id)) {
          this.unavailable(
            "Native Sessionantwort enth\xE4lt keine eindeutige Thread-ID"
          );
          return;
        }
        this.emit({
          nativeThreadId: result.thread.id,
          hasUserTurn: request.method === "thread/resume" || request.method === "thread/fork",
          nativeSessionId: typeof result.thread.sessionId === "string" ? result.thread.sessionId : void 0,
          model: typeof result.model === "string" ? result.model : this.metadata.model,
          reasoningEffort: typeof result.reasoningEffort === "string" ? result.reasoningEffort : this.metadata.reasoningEffort,
          permissionProfile: object(result.activePermissionProfile) && typeof result.activePermissionProfile.id === "string" ? result.activePermissionProfile.id : void 0,
          sandboxMode: object(result.sandbox) && typeof result.sandbox.type === "string" ? result.sandbox.type : void 0,
          cwd: typeof result.cwd === "string" ? result.cwd : void 0,
          runtimeWorkspaceRoots: Array.isArray(result.runtimeWorkspaceRoots) ? result.runtimeWorkspaceRoots.filter(
            (v2) => typeof v2 === "string"
          ) : void 0,
          instructionSources: Array.isArray(result.instructionSources) ? result.instructionSources.filter(
            (v2) => typeof v2 === "string"
          ) : void 0,
          status: "ready",
          identityStatus: "verified",
          error: void 0
        });
        return;
      }
    }
    if (!object(v.params) || v.params.threadId !== this.metadata.nativeThreadId)
      return;
    if (v.method === "turn/started")
      this.emit({ status: "working", hasUserTurn: true });
    if (v.method === "turn/completed") this.emit({ status: "idle" });
    if (v.method === "thread/settings/updated" && object(v.params.threadSettings)) {
      const settings = v.params.threadSettings;
      this.emit({
        model: typeof settings.model === "string" ? settings.model : this.metadata.model,
        reasoningEffort: typeof settings.effort === "string" ? settings.effort : this.metadata.reasoningEffort,
        permissionProfile: object(settings.activePermissionProfile) && typeof settings.activePermissionProfile.id === "string" ? settings.activePermissionProfile.id : this.metadata.permissionProfile,
        sandboxMode: object(settings.sandboxPolicy) && typeof settings.sandboxPolicy.type === "string" ? settings.sandboxPolicy.type : this.metadata.sandboxMode
      });
    }
  }
  exit(code, error) {
    this.emit({ status: error ? "error" : "exited", exitCode: code, error });
  }
};
var WebSocketJsonObserver = class {
  constructor(message, invalid, limit = 8 * 1024 * 1024) {
    this.message = message;
    this.invalid = invalid;
    this.limit = limit;
    this.buffer = Buffer.alloc(0);
    this.upgrade = true;
    this.fragments = [];
    this.size = 0;
    this.disabled = false;
  }
  push(chunk) {
    if (this.disabled) return;
    this.buffer = Buffer.concat([this.buffer, chunk]);
    if (this.buffer.length > this.limit) {
      this.fail(
        "Native Protokollnachricht \xFCberschreitet das Beobachtungslimit"
      );
      return;
    }
    if (this.upgrade) {
      const index = this.buffer.indexOf("\r\n\r\n");
      if (index < 0) return;
      const headers = this.buffer.subarray(0, index).toString();
      if (/Sec-WebSocket-Extensions:/i.test(headers)) {
        this.fail("Komprimierte native WebSocket-Verbindung nicht unterst\xFCtzt");
        return;
      }
      this.buffer = this.buffer.subarray(index + 4);
      this.upgrade = false;
    }
    while (this.buffer.length >= 2) {
      const first = this.buffer[0], second = this.buffer[1];
      const final = (first & 128) !== 0, opcode = first & 15, masked = (second & 128) !== 0;
      let size = second & 127, offset = 2;
      if (first & 112) {
        this.fail("Unbekannte WebSocket-Erweiterung");
        return;
      }
      if (size === 126) {
        if (this.buffer.length < 4) return;
        size = this.buffer.readUInt16BE(2);
        offset = 4;
      } else if (size === 127) {
        if (this.buffer.length < 10) return;
        const length = this.buffer.readBigUInt64BE(2);
        if (length > BigInt(this.limit)) {
          this.fail("Native Protokollnachricht zu gro\xDF");
          return;
        }
        size = Number(length);
        offset = 10;
      }
      if (size > this.limit) {
        this.fail("Native Protokollnachricht zu gro\xDF");
        return;
      }
      const maskOffset = offset;
      offset += masked ? 4 : 0;
      if (this.buffer.length < offset + size) return;
      const payload = Buffer.from(this.buffer.subarray(offset, offset + size));
      if (masked)
        for (let i = 0; i < payload.length; i++)
          payload[i] = payload[i] ^ this.buffer[maskOffset + i % 4];
      this.buffer = this.buffer.subarray(offset + size);
      if (opcode >= 8) continue;
      if (opcode === 1) {
        this.fragments = [];
        this.size = 0;
      } else if (opcode !== 0) {
        this.fail("Unbekannter WebSocket-Datentyp");
        return;
      }
      this.fragments.push(payload);
      this.size += size;
      if (this.size > this.limit) {
        this.fail("Fragmentierte native Nachricht zu gro\xDF");
        return;
      }
      if (final) {
        const text = Buffer.concat(this.fragments).toString("utf8");
        this.fragments = [];
        this.size = 0;
        try {
          this.message(JSON.parse(text));
        } catch {
          this.fail("Native Protokollantwort ist kein JSON");
          return;
        }
      }
    }
  }
  fail(reason) {
    this.disabled = true;
    this.buffer = Buffer.alloc(0);
    this.fragments = [];
    this.invalid(reason);
  }
};

// src/codexProfile.ts
var import_fs3 = require("fs");
var import_path2 = require("path");

// src/vendor/smol-toml/dist/date.js
var DATE_TIME_RE = /^(\d{4}-\d{2}-\d{2})?[T ]?(?:(\d{2}):\d{2}(?::\d{2}(?:\.\d+)?)?)?(Z|[-+]\d{2}:\d{2})?$/i;
var TomlDate = class _TomlDate extends Date {
  #hasDate = false;
  #hasTime = false;
  #offset = null;
  constructor(date) {
    let hasDate = true;
    let hasTime = true;
    let offset = "Z";
    if (typeof date === "string") {
      let match = date.match(DATE_TIME_RE);
      if (match) {
        if (!match[1]) {
          hasDate = false;
          date = `0000-01-01T${date}`;
        }
        hasTime = !!match[2];
        hasTime && date[10] === " " && (date = date.replace(" ", "T"));
        if (match[2] && +match[2] > 23) {
          date = "";
        } else {
          offset = match[3] || null;
          date = date.toUpperCase();
          if (!offset && hasTime)
            date += "Z";
        }
      } else {
        date = "";
      }
    }
    super(date);
    if (!isNaN(this.getTime())) {
      this.#hasDate = hasDate;
      this.#hasTime = hasTime;
      this.#offset = offset;
    }
  }
  isDateTime() {
    return this.#hasDate && this.#hasTime;
  }
  isLocal() {
    return !this.#hasDate || !this.#hasTime || !this.#offset;
  }
  isDate() {
    return this.#hasDate && !this.#hasTime;
  }
  isTime() {
    return this.#hasTime && !this.#hasDate;
  }
  isValid() {
    return this.#hasDate || this.#hasTime;
  }
  toISOString() {
    let iso = super.toISOString();
    if (this.isDate())
      return iso.slice(0, 10);
    if (this.isTime())
      return iso.slice(11, 23);
    if (this.#offset === null)
      return iso.slice(0, -1);
    if (this.#offset === "Z")
      return iso;
    let offset = +this.#offset.slice(1, 3) * 60 + +this.#offset.slice(4, 6);
    offset = this.#offset[0] === "-" ? offset : -offset;
    let offsetDate = new Date(this.getTime() - offset * 6e4);
    return offsetDate.toISOString().slice(0, -1) + this.#offset;
  }
  static wrapAsOffsetDateTime(jsDate, offset = "Z") {
    let date = new _TomlDate(jsDate);
    date.#offset = offset;
    return date;
  }
  static wrapAsLocalDateTime(jsDate) {
    let date = new _TomlDate(jsDate);
    date.#offset = null;
    return date;
  }
  static wrapAsLocalDate(jsDate) {
    let date = new _TomlDate(jsDate);
    date.#hasTime = false;
    date.#offset = null;
    return date;
  }
  static wrapAsLocalTime(jsDate) {
    let date = new _TomlDate(jsDate);
    date.#hasDate = false;
    date.#offset = null;
    return date;
  }
};

// src/vendor/smol-toml/dist/error.js
function getLineColFromPtr(string, ptr) {
  let lines = string.slice(0, ptr).split(/\r\n|\n|\r/g);
  return [lines.length, lines.pop().length + 1];
}
function makeCodeBlock(string, line, column) {
  let lines = string.split(/\r\n|\n|\r/g);
  let codeblock = "";
  let numberLen = (Math.log10(line + 1) | 0) + 1;
  for (let i = line - 1; i <= line + 1; i++) {
    let l = lines[i - 1];
    if (!l)
      continue;
    codeblock += i.toString().padEnd(numberLen, " ");
    codeblock += ":  ";
    codeblock += l;
    codeblock += "\n";
    if (i === line) {
      codeblock += " ".repeat(numberLen + column + 2);
      codeblock += "^\n";
    }
  }
  return codeblock;
}
var TomlError = class extends Error {
  line;
  column;
  codeblock;
  constructor(message, options) {
    const [line, column] = getLineColFromPtr(options.toml, options.ptr);
    const codeblock = makeCodeBlock(options.toml, line, column);
    super(`Invalid TOML document: ${message}

${codeblock}`, options);
    this.line = line;
    this.column = column;
    this.codeblock = codeblock;
  }
};

// src/vendor/smol-toml/dist/util.js
function indexOfNewline(str, start = 0) {
  let idx = str.indexOf("\n", start);
  if (str.charCodeAt(idx - 1) === 13)
    idx--;
  return idx;
}
function skipComment(ctx) {
  for (; ctx.p < ctx.s.length; ctx.p++) {
    let c = ctx.s.charCodeAt(ctx.p);
    if (c === 10)
      break;
    if (c === 13 && ctx.s.charCodeAt(ctx.p + 1) === 10) {
      ctx.p++;
      break;
    }
    if (c < 32 && c !== 9 || c === 127) {
      throw new TomlError("control characters are not allowed in comments", {
        toml: ctx.s,
        ptr: ctx.p
      });
    }
  }
}
function skipVoid(ctx, banNewLines, banComments) {
  let c;
  while (1) {
    while ((c = ctx.s.charCodeAt(ctx.p)) === 32 || c === 9 || !banNewLines && (c === 10 || c === 13 && ctx.s.charCodeAt(ctx.p + 1) === 10))
      ctx.p++;
    if (banComments || c !== 35)
      break;
    skipComment(ctx);
  }
}
function skipUntil(ctx, sep, end) {
  let ptr = ctx.p;
  if (!end) {
    ptr = indexOfNewline(ctx.s, ptr);
    ctx.p = ptr < 0 ? ctx.s.length : ptr;
    return;
  }
  for (; ctx.p < ctx.s.length; ctx.p++) {
    let c = ctx.s.charCodeAt(ctx.p);
    if (c === 35) {
      skipComment(ctx);
    } else if (c === end || c === sep) {
      return;
    }
  }
  throw new TomlError("cannot find end of structure", {
    toml: ctx.s,
    ptr
  });
}

// src/vendor/smol-toml/dist/primitive.js
var INT_REGEX = /^((0x[0-9a-fA-F](_?[0-9a-fA-F])*)|(([+-]|0[ob])?\d(_?\d)*))$/;
var FLOAT_REGEX = /^[+-]?\d(_?\d)*(\.\d(_?\d)*)?([eE][+-]?\d(_?\d)*)?$/;
var LEADING_ZERO = /^[+-]?0[0-9_]/;
function parseString(ctx) {
  let start = ctx.p;
  let c = ctx.s.charCodeAt(ctx.p++);
  let first = c;
  let isLiteral = c === 39;
  let isMultiline = c === ctx.s.charCodeAt(ctx.p) && c === ctx.s.charCodeAt(ctx.p + 1);
  if (isMultiline) {
    if ((c = ctx.s.charCodeAt(ctx.p += 2)) === 10)
      ctx.p++;
    else if (c === 13 && ctx.s.charCodeAt(ctx.p + 1) === 10)
      ctx.p += 2;
  }
  let parsed = "";
  let sliceStart = ctx.p;
  let state = 0;
  for (; ctx.p < ctx.s.length; ctx.p++) {
    c = ctx.s.charCodeAt(ctx.p);
    if (isMultiline && (c === 10 || c === 13 && ctx.s.charCodeAt(ctx.p + 1) === 10)) {
      state = state && 3;
    } else if (c < 32 && c !== 9 || c === 127) {
      throw new TomlError("control characters are not allowed in strings", {
        toml: ctx.s,
        ptr: ctx.p
      });
    } else if ((!state || state === 3) && c === first && (!isMultiline || ctx.s.charCodeAt(ctx.p + 1) === first && ctx.s.charCodeAt(ctx.p + 2) === first)) {
      if (isMultiline) {
        if (ctx.s.charCodeAt(ctx.p + 3) === first)
          ctx.p++;
        if (ctx.s.charCodeAt(ctx.p + 3) === first)
          ctx.p++;
      }
      if (!state)
        parsed += ctx.s.slice(sliceStart, ctx.p);
      ctx.p += isMultiline ? 3 : 1;
      return parsed;
    } else if (!state) {
      if (!isLiteral && c === 92) {
        parsed += ctx.s.slice(sliceStart, sliceStart = ctx.p);
        state = 1;
      }
    } else if (state === 1) {
      if (c === 120 || c === 117 || c === 85) {
        let value = 0;
        let len = c === 120 ? 2 : c === 117 ? 4 : 8;
        for (let j = 0; j < len; j++, ctx.p++) {
          let hex = ctx.s.charCodeAt(ctx.p + 1);
          let digit = (
            /* 0-9 */
            hex >= 48 && hex <= 57 ? hex - 48 : (
              /* A-F */
              hex >= 65 && hex <= 70 ? hex - 65 + 10 : (
                /* a-f */
                hex >= 97 && hex <= 102 ? hex - 97 + 10 : -1
              )
            )
          );
          if (digit < 0)
            throw new TomlError("invalid non-hex character in unicode escape", { toml: ctx.s, ptr: ctx.p + 1 });
          value = value << 4 | digit;
        }
        if (value < 0 || value > 1114111 || value >= 55296 && value <= 57343) {
          throw new TomlError("invalid unicode escape", { toml: ctx.s, ptr: ctx.p });
        }
        parsed += String.fromCodePoint(value);
        sliceStart = ctx.p + 1;
        state = 0;
      } else if (c === 32 || c === 9) {
        state = 2;
      } else {
        if (c === 98)
          parsed += "\b";
        else if (c === 116)
          parsed += "	";
        else if (c === 110)
          parsed += "\n";
        else if (c === 102)
          parsed += "\f";
        else if (c === 114)
          parsed += "\r";
        else if (c === 101)
          parsed += "\x1B";
        else if (c === 34)
          parsed += '"';
        else if (c === 92)
          parsed += "\\";
        else
          throw new TomlError("unrecognized escape sequence", { toml: ctx.s, ptr: ctx.p });
        sliceStart = ctx.p + 1;
        state = 0;
      }
    } else if (c !== 32 && c !== 9) {
      if (state === 2) {
        throw new TomlError("invalid escape: only line-ending whitespace may be escaped", {
          toml: ctx.s,
          ptr: sliceStart
        });
      }
      state = !isLiteral && c === 92 ? 1 : 0;
      sliceStart = ctx.p;
    }
  }
  throw new TomlError("unfinished string", { toml: ctx.s, ptr: start });
}
function sliceAndTrimEndOf(ctx, start, end) {
  let value = ctx.s.slice(start, end);
  let commentIdx = value.indexOf("#");
  if (commentIdx > 0) {
    skipComment({ s: value, p: commentIdx, d: 0 });
    value = value.slice(0, commentIdx);
  }
  return value.trimEnd();
}
function parseValue(ctx, integersAsBigInt, end) {
  let ptr = ctx.p;
  let err = { toml: ctx.s, ptr };
  skipUntil(ctx, 44, end);
  let value = sliceAndTrimEndOf(ctx, ptr, ctx.p);
  if (!value)
    throw new TomlError("incomplete declaration: value expected", err);
  if (value === "-inf")
    return -Infinity;
  if (value === "inf" || value === "+inf")
    return Infinity;
  if (value === "nan" || value === "+nan" || value === "-nan")
    return NaN;
  if (value === "-0")
    return integersAsBigInt ? 0n : 0;
  let isInt = INT_REGEX.test(value);
  if (isInt || FLOAT_REGEX.test(value)) {
    if (LEADING_ZERO.test(value)) {
      throw new TomlError("leading zeroes are not allowed", err);
    }
    value = value.replace(/_/g, "");
    let numeric = +value;
    if (isNaN(numeric)) {
      throw new TomlError("invalid number", err);
    }
    if (isInt) {
      if ((isInt = !Number.isSafeInteger(numeric)) && !integersAsBigInt) {
        throw new TomlError("integer value cannot be represented losslessly", err);
      }
      if (isInt || integersAsBigInt === true)
        numeric = BigInt(value);
    }
    return numeric;
  }
  const date = new TomlDate(value);
  if (!date.isValid())
    throw new TomlError("invalid value", err);
  return date;
}

// src/vendor/smol-toml/dist/extract.js
function extractValue(ctx, end, integersAsBigInt) {
  let ptr = ctx.p;
  let c = ctx.s.charCodeAt(ptr);
  if (c === 91 || c === 123) {
    if (!ctx.d--) {
      throw new TomlError("document contains excessively nested structures. aborting.", {
        toml: ctx.s,
        ptr
      });
    }
    let value = c === 91 ? parseArray(ctx, integersAsBigInt) : parseInlineTable(ctx, integersAsBigInt);
    ctx.d++;
    return value;
  }
  if (c === 34 || c === 39) {
    return parseString(ctx);
  }
  if (c === 116) {
    if (ctx.s.charCodeAt(++ctx.p) !== 114 || ctx.s.charCodeAt(++ctx.p) !== 117 || ctx.s.charCodeAt(++ctx.p) !== 101)
      throw new TomlError("invalid value", { toml: ctx.s, ptr });
    ctx.p++;
    return true;
  }
  if (c === 102) {
    if (ctx.s.charCodeAt(++ctx.p) !== 97 || ctx.s.charCodeAt(++ctx.p) !== 108 || ctx.s.charCodeAt(++ctx.p) !== 115 || ctx.s.charCodeAt(++ctx.p) !== 101)
      throw new TomlError("invalid value", { toml: ctx.s, ptr });
    ctx.p++;
    return false;
  }
  return parseValue(ctx, integersAsBigInt, end);
}

// src/vendor/smol-toml/dist/struct.js
var KEY_PART_RE = /^[a-zA-Z0-9-_]+[ \t]*$/;
function parseKey(ctx, end = "=") {
  let start = ctx.p;
  let dot = start - 1;
  let parsed = [];
  let endPtr = ctx.s.indexOf(end, start);
  if (endPtr < 0) {
    throw new TomlError("incomplete key-value: cannot find end of key", {
      toml: ctx.s,
      ptr: start
    });
  }
  do {
    let c = ctx.s.charCodeAt(ctx.p = ++dot);
    if (c !== 32 && c !== 9) {
      if (c === 34 || c === 39) {
        if (c === ctx.s.charCodeAt(ctx.p + 1) && c === ctx.s.charCodeAt(ctx.p + 2)) {
          throw new TomlError("multiline strings are not allowed in keys", {
            toml: ctx.s,
            ptr: ctx.p
          });
        }
        let part = parseString(ctx);
        dot = ctx.s.indexOf(".", ctx.p);
        let strEnd = ctx.s.slice(ctx.p, dot < 0 || dot > endPtr ? endPtr : dot);
        let newLine = indexOfNewline(strEnd);
        if (newLine > -1) {
          throw new TomlError("newlines are not allowed in keys", {
            toml: ctx.s,
            ptr: newLine
          });
        }
        if (strEnd.trimStart()) {
          throw new TomlError("found extra tokens after the string part", {
            toml: ctx.s,
            ptr: ctx.p
          });
        }
        if (endPtr < ctx.p) {
          endPtr = ctx.s.indexOf(end, ctx.p);
          if (endPtr < 0) {
            throw new TomlError("incomplete key-value: cannot find end of key", {
              toml: ctx.s,
              ptr: start
            });
          }
        }
        parsed.push(part);
      } else {
        dot = ctx.s.indexOf(".", ctx.p);
        let part = ctx.s.slice(ctx.p, dot < 0 || dot > endPtr ? endPtr : dot);
        if (!KEY_PART_RE.test(part)) {
          throw new TomlError("only letter, numbers, dashes and underscores are allowed in keys", {
            toml: ctx.s,
            ptr: ctx.p
          });
        }
        parsed.push(part.trimEnd());
      }
    }
  } while (dot + 1 && dot < endPtr);
  ctx.p = endPtr + 1;
  skipVoid(ctx, true, true);
  return parsed;
}
function parseInlineTable(ctx, integersAsBigInt) {
  let res = {};
  let seen = /* @__PURE__ */ new Set();
  let c;
  ctx.p++;
  while (ctx.p < ctx.s.length) {
    skipVoid(ctx);
    if ((c = ctx.s.charCodeAt(ctx.p)) === 125) {
      ctx.p++;
      return res;
    }
    let k;
    let t = res;
    let hasOwn = false;
    let p = ctx.p;
    let key = parseKey(ctx);
    for (let i = 0; i < key.length; i++) {
      if (i)
        t = hasOwn ? t[k] : t[k] = {};
      k = key[i];
      if ((hasOwn = Object.hasOwn(t, k)) && (typeof t[k] !== "object" || seen.has(t[k]))) {
        throw new TomlError("trying to redefine an already defined value", {
          toml: ctx.s,
          ptr: p
        });
      }
      if (!hasOwn && k === "__proto__") {
        Object.defineProperty(t, k, { enumerable: true, configurable: true, writable: true });
      }
    }
    if (hasOwn) {
      throw new TomlError("trying to redefine an already defined value", {
        toml: ctx.s,
        ptr: ctx.p
      });
    }
    let value = extractValue(ctx, 125, integersAsBigInt);
    seen.add(t[k] = value);
    skipVoid(ctx);
    if ((c = ctx.s.charCodeAt(ctx.p++)) === 125) {
      return res;
    }
    if (c !== 44) {
      throw new TomlError("expected comma or end of structure", { toml: ctx.s, ptr: ctx.p - 1 });
    }
  }
  throw new TomlError("unfinished table encountered", {
    toml: ctx.s,
    ptr: ctx.p
  });
}
function parseArray(ctx, integersAsBigInt) {
  let res = [];
  let c;
  ctx.p++;
  while (ctx.p < ctx.s.length) {
    skipVoid(ctx);
    if ((c = ctx.s.charCodeAt(ctx.p)) === 93) {
      ctx.p++;
      return res;
    }
    res.push(extractValue(ctx, 93, integersAsBigInt));
    skipVoid(ctx);
    if ((c = ctx.s.charCodeAt(ctx.p++)) === 93) {
      return res;
    }
    if (c !== 44) {
      throw new TomlError("expected comma or end of structure", { toml: ctx.s, ptr: ctx.p - 1 });
    }
  }
  throw new TomlError("unfinished array encountered", {
    toml: ctx.s,
    ptr: ctx.p
  });
}

// src/vendor/smol-toml/dist/parse.js
function peekTable(key, table, meta, type) {
  let t = table;
  let m = meta;
  let k;
  let hasOwn = false;
  let state;
  for (let i = 0; i < key.length; i++) {
    if (i) {
      t = hasOwn ? t[k] : t[k] = {};
      m = (state = m[k]).c;
      if (type === 0 && (state.t === 1 || state.t === 2)) {
        return null;
      }
      if (state.t === 2) {
        let l = t.length - 1;
        t = t[l];
        m = m[l].c;
      }
    }
    k = key[i];
    if ((hasOwn = Object.hasOwn(t, k)) && m[k]?.t === 0 && m[k]?.d) {
      return null;
    }
    if (!hasOwn) {
      if (k === "__proto__") {
        Object.defineProperty(t, k, { enumerable: true, configurable: true, writable: true });
        Object.defineProperty(m, k, { enumerable: true, configurable: true, writable: true });
      }
      m[k] = {
        t: i < key.length - 1 && type === 2 ? 3 : type,
        d: false,
        i: 0,
        c: {}
      };
    }
  }
  state = m[k];
  if (state.t !== type && !(type === 1 && state.t === 3)) {
    return null;
  }
  if (type === 2) {
    if (!state.d) {
      state.d = true;
      t[k] = [];
    }
    t[k].push(t = {});
    state.c[state.i++] = state = { t: 1, d: false, i: 0, c: {} };
  }
  if (state.d) {
    return null;
  }
  state.d = true;
  if (type === 1) {
    t = hasOwn ? t[k] : t[k] = {};
  } else if (type === 0 && hasOwn) {
    return null;
  }
  return [k, t, state.c];
}
function parse(toml2, { maxDepth = 1e3, integersAsBigInt } = {}) {
  let ctx = { s: toml2, p: 0, d: maxDepth };
  let res = {};
  let meta = {};
  let tmp;
  let tbl = res;
  let m = meta;
  skipVoid(ctx);
  while (ctx.p < toml2.length) {
    if (toml2.charCodeAt(ctx.p) === 91) {
      let isTableArray = toml2.charCodeAt(++ctx.p) === 91;
      tmp = ctx.p += +isTableArray;
      let k = parseKey(ctx, "]");
      if (isTableArray) {
        if (toml2.charCodeAt(ctx.p - 1) !== 93) {
          throw new TomlError("expected end of table declaration", {
            toml: toml2,
            ptr: ctx.p - 1
          });
        }
        ctx.p++;
      }
      let p = peekTable(
        k,
        res,
        meta,
        isTableArray ? 2 : 1
        /* Type.EXPLICIT */
      );
      if (!p) {
        throw new TomlError("trying to redefine an already defined table or value", {
          toml: toml2,
          ptr: tmp
        });
      }
      m = p[2];
      tbl = p[1];
    } else {
      tmp = ctx.p;
      let k = parseKey(ctx);
      let p = peekTable(
        k,
        tbl,
        m,
        0
        /* Type.DOTTED */
      );
      if (!p) {
        throw new TomlError("trying to redefine an already defined table or value", {
          toml: toml2,
          ptr: tmp
        });
      }
      p[1][p[0]] = extractValue(ctx, void 0, integersAsBigInt);
    }
    skipVoid(ctx, true);
    if (ctx.p < toml2.length && (tmp = toml2.charCodeAt(ctx.p)) !== 10 && tmp !== 13) {
      throw new TomlError("each key-value declaration must be followed by an end-of-line", {
        toml: toml2,
        ptr: ctx.p
      });
    }
    skipVoid(ctx);
  }
  return res;
}

// src/providers/codex.ts
var import_fs2 = require("fs");
var import_path = require("path");
function codexHome() {
  return process.env.CODEX_HOME ?? (0, import_path.join)(homeDir, ".codex");
}
function validateCodexProfile(profile) {
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(profile) || !(0, import_fs2.existsSync)((0, import_path.join)(codexHome(), `${profile}.config.toml`)))
    throw new Error(`Codex-Profil fehlt: ${profile}`);
}

// src/codexProfile.ts
function toml(value) {
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `[${value.map(toml).join(",")}]`;
  if (value instanceof Date) return value.toISOString();
  if (value !== null && typeof value === "object")
    return `{${Object.entries(value).map(([key, v]) => `${JSON.stringify(key)}=${toml(v)}`).join(",")}}`;
  throw new Error("Nicht unterst\xFCtzter TOML-Wert im Codex-Profil");
}
function profileOverrides(profile) {
  if (profile === void 0) return [];
  validateCodexProfile(profile);
  const parsed = parse(
    (0, import_fs3.readFileSync)((0, import_path2.join)(codexHome(), `${profile}.config.toml`), "utf8")
  );
  const result = [];
  const visit = (value, keys) => {
    if (value !== null && typeof value === "object" && !Array.isArray(value) && !(value instanceof Date)) {
      for (const [key, child] of Object.entries(value))
        visit(child, [...keys, key]);
      return;
    }
    result.push(
      "-c",
      `${keys.map((key) => {
        if (!/^[A-Za-z0-9_-]+$/.test(key))
          throw new Error(
            "Nicht unterst\xFCtzter Konfigurationsschl\xFCssel im Codex-Profil"
          );
        return key;
      }).join(".")}=${toml(value)}`
    );
  };
  for (const [key, value] of Object.entries(parsed)) visit(value, [key]);
  return result;
}

// src/codexBridge.ts
async function startCodexBridge(opts) {
  if (process.platform === "win32")
    throw new Error(
      "Codex-Identit\xE4tsbridge ben\xF6tigt Unix-Sockets auf diesem Host"
    );
  const root = (0, import_fs4.mkdtempSync)("/tmp/aos-codex-");
  (0, import_fs4.chmodSync)(root, 448);
  const front = (0, import_path3.join)(root, "tui.sock"), back = (0, import_path3.join)(root, "server.sock");
  const identity = new SessionIdentityObserver(
    opts.tabId,
    opts.launchId,
    opts.onMetadata
  );
  const sockets = /* @__PURE__ */ new Set();
  let closed = false;
  let connected = false;
  let failure;
  const overrides = profileOverrides(opts.profile);
  const child = (0, import_child_process.spawn)(
    codexBinary(),
    ["app-server", "--listen", `unix://${back}`, ...overrides],
    {
      cwd: opts.cwd,
      env: {
        ...spawnEnv(void 0, "codex"),
        AGENTIC_OS_TAB_ID: opts.tabId,
        AGENTIC_OS_LAUNCH_ID: opts.launchId
      },
      detached: true,
      stdio: ["ignore", "ignore", "pipe"]
    }
  );
  let diagnostics = "";
  child.stderr?.on("data", (chunk) => {
    diagnostics = (diagnostics + chunk.toString()).slice(-2048);
  });
  child.on("error", (error) => {
    failure = error.message;
    identity.exit(1, "Codex-App-Server konnte nicht starten");
  });
  child.on("exit", (code) => {
    if (!closed) {
      failure = `Codex-App-Server beendet (${code ?? 1})`;
      identity.exit(code ?? 1, failure);
      for (const socket of sockets) socket.destroy();
    }
  });
  const server = (0, import_net.createServer)((client) => {
    if (connected || closed) {
      client.destroy();
      return;
    }
    connected = true;
    sockets.add(client);
    const backend = (0, import_net.connect)(back);
    sockets.add(backend);
    const clientObserver = new WebSocketJsonObserver(
      (value) => identity.request(value),
      (reason) => identity.unavailable(reason)
    );
    const serverObserver = new WebSocketJsonObserver(
      (value) => identity.response(value),
      (reason) => identity.unavailable(reason)
    );
    client.on("data", (chunk) => clientObserver.push(chunk));
    backend.on("data", (chunk) => serverObserver.push(chunk));
    client.pipe(backend);
    backend.pipe(client);
    client.on("error", () => backend.destroy());
    backend.on("error", () => {
      identity.unavailable(
        "Verbindung zum eigenen Codex-App-Server unterbrochen"
      );
      client.destroy();
    });
    client.on("close", () => {
      backend.destroy();
      dispose();
    });
  });
  const dispose = () => {
    if (closed) return;
    closed = true;
    for (const socket of sockets) socket.destroy();
    server.close();
    try {
      if (child.pid !== void 0) process.kill(-child.pid, "SIGTERM");
    } catch {
    }
    const timer = (0, import_timers.setTimeout)(() => {
      try {
        if (child.pid !== void 0) process.kill(-child.pid, "SIGKILL");
      } catch {
      }
    }, 2500);
    timer.unref();
  };
  try {
    await new Promise((resolve, reject) => {
      const deadline = Date.now() + 15e3;
      const check = () => {
        if (failure !== void 0) {
          reject(new Error(failure));
          return;
        }
        if ((0, import_fs4.existsSync)(back)) {
          resolve();
          return;
        }
        if (Date.now() > deadline) {
          reject(
            new Error(
              "Codex-App-Server startet nicht innerhalb von 15 Sekunden"
            )
          );
          return;
        }
        (0, import_timers.setTimeout)(check, 50);
      };
      check();
    });
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(front, () => {
        (0, import_fs4.chmodSync)(front, 384);
        resolve();
      });
    });
    return { remote: `unix://${front}`, dispose, identity };
  } catch (error) {
    dispose();
    throw error;
  }
}

// src/codexTerminalBridgeCli.ts
async function main() {
  const input = process.argv.slice(2);
  const separator = input.indexOf("--");
  if (separator < 0)
    throw new Error(
      "Usage: codex-terminal-bridge --metadata ABS --launch-id ID --tab-id ID -- [codex args]"
    );
  const options = input.slice(0, separator), args = input.slice(separator + 1);
  const option = (name) => {
    const i = options.indexOf(name);
    return i < 0 ? void 0 : options[i + 1];
  };
  const metadata = option("--metadata"), launchId = option("--launch-id"), tabId = option("--tab-id");
  if (!metadata || !(0, import_path4.isAbsolute)(metadata) || !launchId || !tabId)
    throw new Error("Metadata-Pfad, Start-ID und Tab-ID fehlen");
  let profile, cwd = process.cwd();
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "-p" || arg === "--profile") profile = args[++i];
    if (arg === "-C" || arg === "--cd") cwd = args[++i] ?? cwd;
    if (arg === "--remote" || arg?.startsWith("--remote="))
      throw new Error("Die Bridge verwaltet ihren eigenen lokalen Server");
  }
  (0, import_fs5.mkdirSync)((0, import_path4.dirname)(metadata), { recursive: true, mode: 448 });
  let resumeThreadId = args.includes("resume") ? args[args.indexOf("resume") + 1] : void 0;
  const write = (value) => {
    if (value.identityStatus === "verified")
      resumeThreadId = value.hasUserTurn === false ? void 0 : value.nativeThreadId;
    if ((0, import_fs5.existsSync)(metadata)) {
      try {
        const saved = JSON.parse((0, import_fs5.readFileSync)(metadata, "utf8"));
        if (saved.launchId !== void 0 && saved.launchId !== launchId) return;
      } catch {
      }
    }
    const temporary = `${metadata}.${process.pid}.${(0, import_crypto.randomUUID)()}.tmp`;
    (0, import_fs5.writeFileSync)(
      temporary,
      JSON.stringify({ ...value, resumeThreadId }, null, 2),
      {
        mode: 384,
        flag: "wx"
      }
    );
    (0, import_fs5.renameSync)(temporary, metadata);
  };
  (0, import_fs5.writeFileSync)(
    metadata,
    JSON.stringify({
      version: 1,
      provider: "codex",
      tabId,
      launchId,
      status: "starting",
      resumeThreadId: args.includes("resume") ? args[args.indexOf("resume") + 1] : void 0,
      identityStatus: "pending",
      updatedAt: (/* @__PURE__ */ new Date()).toISOString()
    }),
    { mode: 384 }
  );
  let bridge;
  try {
    bridge = await startCodexBridge({
      tabId,
      launchId,
      profile,
      cwd,
      onMetadata: write
    });
  } catch (error) {
    write({
      version: 1,
      provider: "codex",
      tabId,
      launchId,
      status: "error",
      identityStatus: "pending",
      hasUserTurn: resumeThreadId !== void 0,
      updatedAt: (/* @__PURE__ */ new Date()).toISOString(),
      error: error instanceof Error ? error.message : String(error),
      exitCode: 1
    });
    throw error;
  }
  const child = (0, import_child_process2.spawn)(codexBinary(), ["--remote", bridge.remote, ...args], {
    cwd,
    env: {
      ...spawnEnv(void 0, "codex"),
      AGENTIC_OS_TAB_ID: tabId,
      AGENTIC_OS_LAUNCH_ID: launchId
    },
    stdio: "inherit"
  });
  const stop = (signal) => {
    child.kill(signal);
    bridge.dispose();
  };
  process.once("SIGTERM", () => stop("SIGTERM"));
  process.once("SIGINT", () => stop("SIGINT"));
  child.once("error", (error) => {
    bridge.identity.exit(1, error.message);
    bridge.dispose();
    process.exitCode = 1;
  });
  child.once("exit", (code, signal) => {
    bridge.identity.exit(code ?? (signal ? 130 : 1));
    bridge.dispose();
    process.exitCode = code ?? 1;
  });
}
main().catch((error) => {
  process.stderr.write(
    `[Agentic OS] ${error instanceof Error ? error.message : String(error)}
`
  );
  process.exitCode = 1;
});
/*!
 * Copyright (c) Squirrel Chat et al., All rights reserved.
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 *
 * 1. Redistributions of source code must retain the above copyright notice, this
 *    list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright notice,
 *    this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 * 3. Neither the name of the copyright holder nor the names of its contributors
 *    may be used to endorse or promote products derived from this software without
 *    specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
 * WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
 * DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
 * SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
 * CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
 * OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */
