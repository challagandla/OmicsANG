/* SPDX-FileCopyrightText: 2026 Anil Kumar Challagandla */
/* SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0 */
/* Dependency-free editing primitives shared by the OmicsANG browser editor and tests. */
(function exposeEditorCore(root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.OmicsEditorCore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function createEditorCore() {
  'use strict';

  const SUPPORTED_TAB_SIZES = new Set([2, 4, 8]);
  const COMPLETION_SCAN_LIMIT = 120000;
  const COMPLETION_MATCH_LIMIT = 2048;
  const COMPLETION_CANDIDATE_LIMIT = 256;
  const COMPLETION_IDENTIFIER_LIMIT = 80;
  const EXPLANATION_LINE_LIMIT = 2000;
  const COMMON_COMPLETIONS = Object.freeze([
    'false', 'null', 'true',
  ]);
  const LANGUAGE_COMPLETIONS = Object.freeze({
    python: Object.freeze([
      'async', 'await', 'class', 'def', 'elif', 'else', 'except', 'finally',
      'for', 'from', 'if', 'import', 'isinstance', 'lambda', 'raise', 'return',
      'self', 'try', 'while', 'with', 'yield',
    ]),
    snakemake: Object.freeze([
      'benchmark', 'checkpoint', 'conda', 'container', 'input', 'log', 'output',
      'params', 'resources', 'rule', 'ruleorder', 'script', 'shell', 'temp',
      'threads', 'wildcard_constraints', 'workflow',
    ]),
    yaml: Object.freeze([
      'config', 'environment', 'input', 'metadata', 'output', 'params',
      'resources', 'samples', 'threads', 'workflow',
    ]),
    shell: Object.freeze([
      'case', 'done', 'elif', 'else', 'export', 'fi', 'for', 'function', 'if',
      'local', 'printf', 'then', 'while',
    ]),
    r: Object.freeze([
      'else', 'for', 'function', 'if', 'library', 'next',
      'require', 'return', 'while',
    ]),
    javascript: Object.freeze([
      'async', 'await', 'class', 'const', 'else', 'export', 'extends',
      'function', 'if', 'import', 'let', 'new', 'return', 'throw', 'try',
    ]),
    typescript: Object.freeze([
      'async', 'await', 'class', 'const', 'else', 'export', 'extends',
      'function', 'if', 'implements', 'import', 'interface', 'let', 'new',
      'return', 'throw', 'try', 'type',
    ]),
    json: Object.freeze(['false', 'null', 'true']),
    jsonl: Object.freeze(['false', 'null', 'true']),
    toml: Object.freeze(['false', 'true']),
    markdown: Object.freeze([]),
    css: Object.freeze(['display']),
    html: Object.freeze(['class', 'data', 'id', 'role']),
  });

  function normalizedLanguage(language = '', path = '') {
    const requested = String(language || '').trim().toLowerCase();
    const aliases = {
      bash: 'shell', js: 'javascript', jsonlines: 'jsonl', py: 'python',
      rscript: 'r', sh: 'shell', snake: 'snakemake', ts: 'typescript',
      yml: 'yaml', zsh: 'shell',
    };
    if (requested && requested !== 'text' && requested !== 'plain') {
      return aliases[requested] || requested;
    }
    const file = String(path || '').toLowerCase();
    const name = file.split('/').pop() || '';
    if (name === 'snakefile' || file.endsWith('.smk')) return 'snakemake';
    if (file.endsWith('.py')) return 'python';
    if (file.endsWith('.r')) return 'r';
    if (/\.(?:sh|bash|zsh)$/.test(file)) return 'shell';
    if (/\.ya?ml$/.test(file)) return 'yaml';
    if (/\.(?:js|mjs|cjs|jsx)$/.test(file)) return 'javascript';
    if (/\.(?:ts|mts|cts|tsx)$/.test(file)) return 'typescript';
    if (file.endsWith('.jsonl')) return 'jsonl';
    if (file.endsWith('.json')) return 'json';
    if (file.endsWith('.toml')) return 'toml';
    if (/\.(?:md|markdown)$/.test(file)) return 'markdown';
    if (file.endsWith('.css')) return 'css';
    if (/\.html?$/.test(file)) return 'html';
    return requested === 'text' || requested === 'plain' ? '' : requested;
  }

  function normalizeTabSize(value, fallback = 4) {
    const cleanFallback = SUPPORTED_TAB_SIZES.has(Number(fallback)) ? Number(fallback) : 4;
    return SUPPORTED_TAB_SIZES.has(Number(value)) ? Number(value) : cleanFallback;
  }

  function defaultIndentSize(language = '', path = '') {
    const mode = String(language || '').toLowerCase();
    const file = String(path || '').toLowerCase();
    if (['yaml', 'json', 'jsonl', 'javascript', 'typescript', 'css', 'html'].includes(mode)) return 2;
    if (/\.(?:ya?ml|jsonl?|[cm]?[jt]sx?|css|html?)$/.test(file)) return 2;
    return 4;
  }

  function leadingWhitespace(line) {
    return (String(line || '').match(/^[ \t]*/) || [''])[0];
  }

  function visualWidth(text, tabSize = 4) {
    const size = normalizeTabSize(tabSize);
    let column = 0;
    for (const char of String(text || '')) {
      if (char === '\t') column += size - (column % size);
      else column += 1;
    }
    return column;
  }

  function indentationColumns(line, tabSize = 4) {
    return visualWidth(leadingWhitespace(line), tabSize);
  }

  function detectIndentation(text, fallbackSize = 4) {
    const fallback = normalizeTabSize(fallbackSize);
    const indents = [];
    let tabLines = 0;
    let spaceLines = 0;
    for (const line of String(text || '').split('\n').slice(0, 2000)) {
      if (!line.trim()) continue;
      const leading = leadingWhitespace(line);
      if (!leading) continue;
      if (leading[0] === '\t') tabLines += 1;
      else {
        spaceLines += 1;
        if (leading.length >= 2 && leading.length <= 80) indents.push(leading.length);
      }
    }
    if (tabLines > spaceLines) return { mode: 'tabs', size: fallback };
    if (!indents.length) return { mode: 'spaces', size: fallback };

    const minimumIndent = Math.min(...indents);
    // A real 2- or 4-column first level is stronger evidence than the number
    // of repeated deeper-level lines (for example, many sibling YAML keys).
    if (minimumIndent === 2 || minimumIndent === 4) {
      return { mode: 'spaces', size: minimumIndent };
    }
    let bestSize = fallback === 8 ? 4 : fallback;
    let bestScore = -Infinity;
    // Auto-detection intentionally chooses the two common source-code units.
    // Eight remains available as an explicit display/editing choice.
    for (const size of [2, 4]) {
      let score = size === fallback ? 0.25 : 0;
      if (minimumIndent === size) score += 12;
      for (const indent of indents) {
        if (indent === size) score += 5;
        else if (indent % size === 0) score += 1;
        else score -= 2;
      }
      if (score > bestScore) {
        bestScore = score;
        bestSize = size;
      }
    }
    return { mode: 'spaces', size: bestSize };
  }

  function clampOffset(text, value) {
    const length = String(text || '').length;
    return Math.max(0, Math.min(length, Number(value) || 0));
  }

  function lineStarts(text) {
    const source = String(text || '');
    const starts = [0];
    for (let i = 0; i < source.length; i += 1) {
      if (source[i] === '\n') starts.push(i + 1);
    }
    return starts;
  }

  function lineIndexAt(starts, offset) {
    let low = 0;
    let high = starts.length - 1;
    while (low <= high) {
      const mid = Math.floor((low + high) / 2);
      if (starts[mid] <= offset) low = mid + 1;
      else high = mid - 1;
    }
    return Math.max(0, high);
  }

  function cursorPositionFromStarts(text, offset, starts, tabSize = 4) {
    const source = String(text || '');
    const point = clampOffset(source, offset);
    const anchors = Array.isArray(starts) && starts.length ? starts : lineStarts(source);
    const lineIndex = lineIndexAt(anchors, point);
    const lineStart = anchors[lineIndex];
    const prefix = source.slice(lineStart, point);
    return {
      line: lineIndex + 1,
      lineIndex,
      column: prefix.length + 1,
      visualColumn: visualWidth(prefix, tabSize) + 1,
      lineStart,
    };
  }

  function cursorPosition(text, offset, tabSize = 4) {
    const source = String(text || '');
    return cursorPositionFromStarts(source, offset, lineStarts(source), tabSize);
  }

  function stripCommandComment(line) {
    const source = String(line || '');
    let quote = '';
    let escaped = false;
    for (let index = 0; index < source.length; index += 1) {
      const char = source[index];
      if (escaped) {
        escaped = false;
        continue;
      }
      if (char === '\\') {
        escaped = true;
        continue;
      }
      if (quote) {
        if (char === quote) quote = '';
        continue;
      }
      if (char === '"' || char === "'" || char === '`') {
        quote = char;
        continue;
      }
      if (char === '#' && (index === 0 || /\s/.test(source[index - 1]))) {
        return source.slice(0, index);
      }
    }
    return source;
  }

  function commandOptionAt(line, offset) {
    const source = String(line || '');
    const point = Math.max(0, Math.min(source.length, Number(offset) || 0));
    const optionPattern = /(^|[\s"'`])(--?[A-Za-z][A-Za-z0-9_-]*)(?:=[^\s"'`]*)?/g;
    let match;
    while ((match = optionPattern.exec(source)) !== null) {
      const start = match.index + match[1].length;
      const end = optionPattern.lastIndex;
      if (point >= start && point <= end) return match[2];
    }
    // When the caret is on an option value, retain the immediately preceding
    // flag.  This intentionally handles only one shell-like token, not a full
    // shell grammar.
    const before = source.slice(0, point);
    const valueMatch = before.match(/(--?[A-Za-z][A-Za-z0-9_-]*)(?:=[^\s]*)?\s+[^\s]*$/);
    return valueMatch ? valueMatch[1] : '';
  }

  function commandNameInLine(line, knownCommands, beforeOffset = Infinity) {
    const source = stripCommandComment(String(line || '').slice(0, 20000));
    const limit = Math.max(0, Math.min(source.length, Number(beforeOffset) || 0));
    if (!source.trim() || source.trimStart().startsWith('#')) return '';
    const tokens = /(^|[\s;|&("'`])([^\s;|&()"'`\\]+)/g;
    let match;
    let found = '';
    while ((match = tokens.exec(source)) !== null) {
      const start = match.index + match[1].length;
      if (start > limit) break;
      const raw = match[2].replace(/^[{[]+/, '').replace(/[}\]]+$/, '');
      const name = raw.split('/').filter(Boolean).pop()?.toLowerCase() || '';
      if (knownCommands.has(name)) found = name;
    }
    return found;
  }

  function commandContextAt(text, offset, knownCommands = []) {
    const source = String(text || '');
    const commands = new Set(
      [...knownCommands].slice(0, 32)
        .map(name => String(name || '').trim().toLowerCase())
        .filter(name => /^[a-z0-9][a-z0-9_.+-]{0,63}$/.test(name)),
    );
    if (!commands.size) return null;
    const point = clampOffset(source, offset);
    const starts = lineStarts(source);
    const current = lineIndexAt(starts, point);
    const currentStart = starts[current];
    const currentEnd = source.indexOf('\n', currentStart);
    const rawCurrentLine = source.slice(currentStart, currentEnd === -1 ? source.length : currentEnd);
    const currentLine = stripCommandComment(rawCurrentLine.slice(0, 20000));
    const localOffset = Math.min(currentLine.length, point - currentStart);
    if (rawCurrentLine.trimStart().startsWith('#') || point - currentStart > currentLine.length) return null;

    let blankLines = 0;
    for (let index = current; index >= Math.max(0, current - 15); index -= 1) {
      const start = starts[index];
      const end = source.indexOf('\n', start);
      const line = source.slice(start, end === -1 ? source.length : end);
      if (!line.trim()) {
        blankLines += 1;
        if (blankLines >= 2) break;
        continue;
      }
      blankLines = 0;
      const command = commandNameInLine(
        line,
        commands,
        index === current ? localOffset : Infinity,
      );
      if (!command) continue;
      return {
        command,
        option: commandOptionAt(currentLine, localOffset),
        line: current + 1,
        commandLine: index + 1,
        context: currentLine.trim().slice(0, 500),
      };
    }
    return null;
  }

  function escapeRegExp(value) {
    return String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function inlineCompletionAt(text, offset, options = {}) {
    const source = String(text || '');
    const point = clampOffset(source, offset);
    // Inserting a suffix while the caret is inside an identifier would duplicate
    // its existing tail. Fail closed and wait until the caret reaches the end.
    if (/[A-Za-z0-9_]/.test(source[point] || '')) return null;
    const before = source.slice(Math.max(0, point - COMPLETION_IDENTIFIER_LIMIT), point);
    const prefixMatch = before.match(/[A-Za-z_][A-Za-z0-9_]*$/);
    if (!prefixMatch || prefixMatch[0].length < 2) return null;
    const prefix = prefixMatch[0];
    if (prefix.length >= COMPLETION_IDENTIFIER_LIMIT) return null;
    const tokenStart = point - prefix.length;

    // Search a bounded window biased toward text before the caret, where a
    // declaration is most likely to live. Boundary checks use the full source
    // so a window cut through a long identifier cannot create a false token.
    let scanStart = Math.max(0, point - Math.floor(COMPLETION_SCAN_LIMIT * 2 / 3));
    let scanEnd = Math.min(source.length, scanStart + COMPLETION_SCAN_LIMIT);
    if (scanEnd - scanStart < COMPLETION_SCAN_LIMIT) {
      scanStart = Math.max(0, scanEnd - COMPLETION_SCAN_LIMIT);
    }
    const corpus = source.slice(scanStart, scanEnd);
    const candidates = new Map();
    const pattern = new RegExp(
      `(^|[^A-Za-z0-9_])(${escapeRegExp(prefix)}[A-Za-z0-9_]{1,${COMPLETION_IDENTIFIER_LIMIT - 1}})(?![A-Za-z0-9_])`,
      'g',
    );
    let match;
    let matched = 0;
    while (matched < COMPLETION_MATCH_LIMIT && (match = pattern.exec(corpus)) !== null) {
      matched += 1;
      const candidate = match[2];
      const absoluteStart = scanStart + match.index + match[1].length;
      const absoluteEnd = absoluteStart + candidate.length;
      const fullBoundaryBefore = source[absoluteStart - 1] || '';
      const fullBoundaryAfter = source[absoluteEnd] || '';
      if (/[A-Za-z0-9_]/.test(fullBoundaryBefore) || /[A-Za-z0-9_]/.test(fullBoundaryAfter)) continue;
      if (candidate === prefix || candidate.length > COMPLETION_IDENTIFIER_LIMIT) continue;
      const previous = candidates.get(candidate);
      const distance = Math.abs(absoluteStart - tokenStart);
      if (previous) {
        previous.count += 1;
        previous.distance = Math.min(previous.distance, distance);
      } else if (candidates.size < COMPLETION_CANDIDATE_LIMIT) {
        candidates.set(candidate, { candidate, count: 1, distance });
      }
    }

    const documentCandidate = [...candidates.values()].sort((left, right) =>
      left.distance - right.distance ||
      right.count - left.count ||
      left.candidate.length - right.candidate.length ||
      left.candidate.localeCompare(right.candidate))[0];
    let candidate = documentCandidate ? documentCandidate.candidate : '';
    let completionSource = documentCandidate ? 'document' : '';

    if (!candidate) {
      const mode = normalizedLanguage(options.language, options.path);
      const literalVocabulary = mode && !['markdown', 'css', 'html'].includes(mode)
        ? COMMON_COMPLETIONS : [];
      const vocabulary = [...(LANGUAGE_COMPLETIONS[mode] || []), ...literalVocabulary];
      candidate = vocabulary.find(item =>
        item.length <= COMPLETION_IDENTIFIER_LIMIT &&
        item !== prefix &&
        item.startsWith(prefix) &&
        /^[A-Za-z_][A-Za-z0-9_]*$/.test(item)) || '';
      completionSource = candidate ? 'language' : '';
    }

    if (!candidate) return null;
    const suffix = candidate.slice(prefix.length);
    if (!suffix || !/^[A-Za-z0-9_]+$/.test(suffix)) return null;
    return {
      prefix,
      candidate,
      suffix,
      source: completionSource,
      start: tokenStart,
      end: point,
    };
  }

  function explainLine(line, options = {}) {
    const raw = String(line || '').replace(/\r$/, '').slice(0, EXPLANATION_LINE_LIMIT);
    const text = raw.trim();
    const mode = normalizedLanguage(options.language, options.path);
    const requestedLine = Number(options.lineNumber);
    const lineNumber = Number.isFinite(requestedLine) && requestedLine > 0
      ? Math.min(10000000, Math.trunc(requestedLine)) : 0;
    const result = summary => `${lineNumber ? `Line ${lineNumber}: ` : ''}${summary}`;
    const quoted = value => `"${String(value || '').slice(0, 80)}"`;

    if (!text) return result('Blank line separating nearby code or data.');

    const markdownHeading = mode === 'markdown' && text.match(/^(#{1,6})\s+(.{1,120})$/);
    if (markdownHeading) {
      return result(`Markdown level-${markdownHeading[1].length} heading ${quoted(markdownHeading[2])}.`);
    }
    if (mode === 'markdown' && /^(?:[-*+] |\d+[.)]\s)/.test(text)) {
      return result('Markdown list item.');
    }

    const hashComment = ['python', 'r', 'shell', 'snakemake', 'yaml', 'toml', ''].includes(mode) &&
      text.startsWith('#');
    const slashComment = ['javascript', 'typescript', 'css'].includes(mode) &&
      (/^\/\//.test(text) || /^\/\*/.test(text) || /^\*/.test(text));
    const htmlComment = mode === 'html' && text.startsWith('<!--');
    if (hashComment || slashComment || htmlComment) return result('Comment for human context; it is not executed as code.');

    if ((mode === 'shell' || mode === 'snakemake') &&
        (/(^|\s)(?:sudo\s+)?rm\s+(?:-[A-Za-z]*r[A-Za-z]*f|-[A-Za-z]*f[A-Za-z]*r)(?:\s|$)/.test(text) ||
         /curl\b[^|]*\|\s*(?:ba)?sh\b/.test(text) ||
         /chmod\s+777\b/.test(text))) {
      return result('Potentially destructive shell operation; verify its paths and inputs before running it.');
    }

    if (mode === 'snakemake') {
      const rule = text.match(/^(?:rule|checkpoint)\s+([A-Za-z_][A-Za-z0-9_-]*)\s*:/);
      if (rule) return result(`Defines the Snakemake rule or checkpoint ${quoted(rule[1])}.`);
      const directive = text.match(/^(input|output|params|threads|resources|log|benchmark|conda|container|shell|script|notebook|run|wildcard_constraints)\s*:/);
      if (directive) {
        const descriptions = {
          input: 'Declares inputs consumed by the current Snakemake rule.',
          output: 'Declares files produced by the current Snakemake rule.',
          params: 'Declares non-file parameters for the current Snakemake rule.',
          threads: 'Sets the thread request for the current Snakemake rule.',
          resources: 'Sets scheduler or runtime resources for the current Snakemake rule.',
          log: 'Declares log output for the current Snakemake rule.',
          benchmark: 'Declares benchmark output for the current Snakemake rule.',
          conda: 'Selects the Conda environment for the current Snakemake rule.',
          container: 'Selects the container image for the current Snakemake rule.',
          shell: 'Runs a shell command as the action of the current Snakemake rule.',
          script: 'Runs an external script as the action of the current Snakemake rule.',
          notebook: 'Runs a notebook as the action of the current Snakemake rule.',
          run: 'Begins an inline Python action for the current Snakemake rule.',
          wildcard_constraints: 'Restricts wildcard values accepted by the current Snakemake rule.',
        };
        return result(descriptions[directive[1]]);
      }
    }

    if (mode === 'python') {
      let found = text.match(/^(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/);
      if (found) return result(`Defines the Python function ${quoted(found[1])}.`);
      found = text.match(/^class\s+([A-Za-z_][A-Za-z0-9_]*)\b/);
      if (found) return result(`Defines the Python class ${quoted(found[1])}.`);
      found = text.match(/^from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\b/);
      if (found) return result(`Imports names from the Python module ${quoted(found[1])}.`);
      found = text.match(/^import\s+([A-Za-z_][A-Za-z0-9_.]*)\b/);
      if (found) return result(`Imports the Python module ${quoted(found[1])}.`);
      found = text.match(/^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=(?!=)/);
      if (found) return result(`Assigns a value to the Python name ${quoted(found[1])}.`);
      if (/^return\b/.test(text)) return result('Returns a value from the current Python function.');
      if (/^raise\b/.test(text)) return result('Raises a Python exception and interrupts normal control flow.');
      if (/^(?:if|elif)\b/.test(text)) return result('Tests a condition and controls which Python block runs.');
      if (/^(?:for|while)\b/.test(text)) return result('Begins a Python loop.');
      if (/^(?:try|except|finally)\b/.test(text)) return result('Handles Python exceptions or cleanup control flow.');
      if (/^with\b/.test(text)) return result('Enters a Python context that manages setup and cleanup.');
    }

    if (mode === 'javascript' || mode === 'typescript') {
      let found = text.match(/^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(/);
      if (found) return result(`Defines the ${mode === 'typescript' ? 'TypeScript' : 'JavaScript'} function ${quoted(found[1])}.`);
      found = text.match(/^(?:export\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b/);
      if (found) return result(`Defines the ${mode === 'typescript' ? 'TypeScript' : 'JavaScript'} class ${quoted(found[1])}.`);
      found = text.match(/^(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b/);
      if (found) return result(`Declares the ${mode === 'typescript' ? 'TypeScript' : 'JavaScript'} variable ${quoted(found[1])}.`);
      if (/^import\b/.test(text)) return result(`Imports a ${mode === 'typescript' ? 'TypeScript' : 'JavaScript'} dependency.`);
      if (/^export\b/.test(text)) return result('Exports a declaration for use by another module.');
    }

    if (mode === 'r') {
      let found = text.match(/^(?:library|require)\s*\(\s*["']?([A-Za-z][A-Za-z0-9.]*)/);
      if (found) return result(`Loads the R package ${quoted(found[1])}.`);
      found = text.match(/^([A-Za-z.][A-Za-z0-9._]*)\s*<-\s*function\b/);
      if (found) return result(`Defines the R function ${quoted(found[1])}.`);
      found = text.match(/^([A-Za-z.][A-Za-z0-9._]*)\s*(?:<-|=(?!=))/);
      if (found) return result(`Assigns a value to the R name ${quoted(found[1])}.`);
    }

    if (mode === 'shell') {
      let found = text.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=/);
      if (found) return result(`Sets the shell variable ${quoted(found[1])}.`);
      if (/^set\s+-[^\n]*e/.test(text)) return result('Enables stricter shell error handling; exact flags determine failure behavior.');
      if (/^(?:if|elif|case)\b/.test(text)) return result('Tests shell conditions and controls which branch runs.');
      if (/^(?:for|while|until)\b/.test(text)) return result('Begins a shell loop.');
      if (text.includes('|')) return result('Pipes one command\'s output into another command.');
      found = text.match(/^(?:command\s+|exec\s+)?([A-Za-z0-9_./+-]+)/);
      if (found) return result(`Runs the shell command ${quoted(found[1].split('/').pop())}.`);
    }

    if (mode === 'yaml') {
      const mapping = text.match(/^([A-Za-z0-9_.-]+)\s*:\s*(.*)$/);
      if (mapping) return result(mapping[2]
        ? `Sets the YAML key ${quoted(mapping[1])}.`
        : `Opens the YAML mapping ${quoted(mapping[1])}.`);
      if (/^-\s+/.test(text)) return result('Adds an item to a YAML sequence.');
    }

    if (mode === 'toml') {
      const section = text.match(/^\[([^\]]{1,120})\]$/);
      if (section) return result(`Opens the TOML section ${quoted(section[1])}.`);
      const key = text.match(/^([A-Za-z0-9_.-]+)\s*=/);
      if (key) return result(`Sets the TOML key ${quoted(key[1])}.`);
    }

    if (mode === 'json' || mode === 'jsonl') {
      const key = text.match(/^"([^"\\]{1,120})"\s*:/);
      if (key) return result(`Sets the JSON property ${quoted(key[1])}.`);
    }

    const assignment = text.match(/^([A-Za-z_][A-Za-z0-9_.-]*)\s*(?:<-|=(?!=)|:)/);
    if (assignment) return result(`Assigns or labels the value ${quoted(assignment[1])}.`);
    const call = text.match(/^(?:await\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*\(/);
    if (call) return result(`Calls ${quoted(call[1])}; surrounding code determines its inputs and effects.`);
    if (/^[}\])]+[;,]?$/.test(text)) return result('Closes the current code or data block.');
    return result(`Contains a ${mode || 'code'} statement; inspect surrounding lines for its exact role.`);
  }

  function guideColumns(line, tabSize = 4) {
    const size = normalizeTabSize(tabSize);
    const indentation = indentationColumns(line, size);
    const columns = [];
    for (let column = size; column <= indentation; column += size) columns.push(column);
    return columns;
  }

  function activeIndentScope(lines, lineIndex, tabSize = 4) {
    const rows = Array.isArray(lines) ? lines : String(lines || '').split('\n');
    const size = normalizeTabSize(tabSize);
    const current = Math.max(0, Math.min(rows.length - 1, Math.trunc(Number(lineIndex) || 0)));
    let reference = current;
    while (reference > 0 && String(rows[reference] || '').length === 0) reference -= 1;
    const depth = indentationColumns(rows[reference] || '', size);
    const column = Math.floor(depth / size) * size;
    if (!column) return { column: 0, start: current, end: current };

    let start = current;
    for (let index = current - 1; index >= 0; index -= 1) {
      const line = String(rows[index] || '');
      if (!line.trim() || indentationColumns(line, size) >= column) start = index;
      else break;
    }
    let end = current;
    for (let index = current + 1; index < rows.length; index += 1) {
      const line = String(rows[index] || '');
      if (!line.trim() || indentationColumns(line, size) >= column) end = index;
      else break;
    }
    return { column, start, end };
  }

  function selectedLineStarts(text, start, end) {
    const source = String(text || '');
    const cleanStart = clampOffset(source, start);
    const cleanEnd = Math.max(cleanStart, clampOffset(source, end));
    const first = cleanStart === 0 ? 0 : source.lastIndexOf('\n', cleanStart - 1) + 1;
    let effectiveEnd = cleanEnd;
    if (cleanEnd > cleanStart && source[cleanEnd - 1] === '\n') effectiveEnd -= 1;
    const starts = [first];
    let cursor = source.indexOf('\n', first);
    while (cursor !== -1 && cursor < effectiveEnd) {
      starts.push(cursor + 1);
      cursor = source.indexOf('\n', cursor + 1);
    }
    return starts;
  }

  function mapOffset(offset, edits, includeAt = false) {
    let delta = 0;
    for (const edit of edits) {
      const editEnd = edit.at + edit.remove;
      if (offset < edit.at) break;
      if (edit.remove === 0 && offset === edit.at) {
        return edit.at + delta + (includeAt ? edit.insert.length : 0);
      }
      if (offset === editEnd) return edit.at + delta + edit.insert.length;
      if (offset < editEnd) return edit.at + delta + (includeAt ? edit.insert.length : 0);
      delta += edit.insert.length - edit.remove;
    }
    return offset + delta;
  }

  function mapCollapsedOffset(offset, edits) {
    let delta = 0;
    for (const edit of edits) {
      const editEnd = edit.at + edit.remove;
      if (offset < edit.at) break;
      if (offset === edit.at) return edit.at + delta;
      if (offset <= editEnd) {
        const projected = offset - edit.at + edit.insert.length - edit.remove;
        return edit.at + delta + Math.max(0, Math.min(edit.insert.length, projected));
      }
      delta += edit.insert.length - edit.remove;
    }
    return offset + delta;
  }

  function applyEdits(text, edits) {
    const source = String(text || '');
    let cursor = 0;
    let output = '';
    for (const edit of edits) {
      output += source.slice(cursor, edit.at) + edit.insert;
      cursor = edit.at + edit.remove;
    }
    return output + source.slice(cursor);
  }

  function indentationUnit(mode, size) {
    return mode === 'tabs' ? '\t' : ' '.repeat(normalizeTabSize(size));
  }

  function applyIndentEdit(text, start, end, options = {}) {
    const source = String(text || '');
    const cleanStart = clampOffset(source, start);
    const cleanEnd = Math.max(cleanStart, clampOffset(source, end));
    const size = normalizeTabSize(options.size);
    const mode = options.mode === 'tabs' ? 'tabs' : 'spaces';
    const outdent = !!options.outdent;

    if (!outdent && cleanStart === cleanEnd) {
      const lineStart = cleanStart === 0 ? 0 : source.lastIndexOf('\n', cleanStart - 1) + 1;
      const before = source.slice(lineStart, cleanStart);
      const width = visualWidth(before, size);
      const insert = mode === 'tabs' ? '\t' : ' '.repeat(size - (width % size || 0));
      return {
        value: source.slice(0, cleanStart) + insert + source.slice(cleanEnd),
        start: cleanStart + insert.length,
        end: cleanStart + insert.length,
        handled: true,
      };
    }

    const starts = selectedLineStarts(source, cleanStart, cleanEnd);
    const unit = indentationUnit(mode, size);
    const edits = [];
    for (const at of starts) {
      if (!outdent) {
        edits.push({ at, remove: 0, insert: unit });
        continue;
      }
      const leading = leadingWhitespace(source.slice(at));
      if (!leading) continue;
      const columns = visualWidth(leading, size);
      const target = Math.max(0, columns - (columns % size || size));
      const insert = mode === 'tabs'
        ? '\t'.repeat(Math.floor(target / size)) + ' '.repeat(target % size)
        : ' '.repeat(target);
      edits.push({ at, remove: leading.length, insert });
    }
    if (!edits.length) return { value: source, start: cleanStart, end: cleanEnd, handled: true };
    const value = applyEdits(source, edits);
    if (cleanStart === cleanEnd) {
      const caret = mapCollapsedOffset(cleanStart, edits);
      return { value, start: caret, end: caret, handled: true };
    }
    return {
      value,
      start: mapOffset(cleanStart, edits, false),
      end: mapOffset(cleanEnd, edits, true),
      handled: true,
    };
  }

  function shouldIncreaseIndent(before, language = '', path = '') {
    const text = String(before || '').replace(/\s+$/, '');
    if (!text) return false;
    if (/[{[(]$/.test(text)) return true;
    const mode = String(language || '').toLowerCase();
    const file = String(path || '').toLowerCase();
    const indentationSensitive = ['python', 'yaml', 'snakemake'].includes(mode) ||
      /\.(?:py|ya?ml|smk)$/.test(file) || /(?:^|\/)snakefile$/.test(file);
    if (text.endsWith(':') && indentationSensitive) return true;
    if (text.endsWith(':') && ['javascript', 'typescript'].includes(mode) &&
        /\b(?:case\b.*|default):$/.test(text)) return true;
    if (mode === 'shell' || /\.(?:sh|bash|zsh)$/.test(file)) {
      return /\b(?:then|do|else)\s*$/.test(text);
    }
    return false;
  }

  function applyEnterEdit(text, start, end, options = {}) {
    const source = String(text || '');
    const cleanStart = clampOffset(source, start);
    const cleanEnd = Math.max(cleanStart, clampOffset(source, end));
    const size = normalizeTabSize(options.size);
    const mode = options.mode === 'tabs' ? 'tabs' : 'spaces';
    const lineStart = cleanStart === 0 ? 0 : source.lastIndexOf('\n', cleanStart - 1) + 1;
    const before = source.slice(lineStart, cleanStart);
    const indent = leadingWhitespace(before);
    const extra = shouldIncreaseIndent(before, options.language, options.path)
      ? indentationUnit(mode, size) : '';
    const trimmed = before.replace(/\s+$/, '');
    const opener = trimmed.slice(-1);
    const closer = { '(': ')', '[': ']', '{': '}' }[opener];
    const paired = closer && source.slice(cleanEnd).startsWith(closer);
    const insert = paired
      ? `\n${indent}${extra}\n${indent}`
      : `\n${indent}${extra}`;
    const caret = cleanStart + 1 + indent.length + extra.length;
    return {
      value: source.slice(0, cleanStart) + insert + source.slice(cleanEnd),
      start: caret,
      end: caret,
      handled: true,
    };
  }

  function lineCommentToken(language = '', path = '') {
    const mode = String(language || '').toLowerCase();
    const file = String(path || '').toLowerCase();
    if (['javascript', 'typescript'].includes(mode) || /\.[cm]?[jt]sx?$/.test(file)) return '//';
    if (['python', 'r', 'shell', 'yaml', 'toml', 'snakemake'].includes(mode)) return '#';
    if (/\.(?:py|r|sh|bash|zsh|ya?ml|toml|smk)$/.test(file) || file.endsWith('snakefile')) return '#';
    return '';
  }

  function toggleLineComment(text, start, end, options = {}) {
    const source = String(text || '');
    const cleanStart = clampOffset(source, start);
    const cleanEnd = Math.max(cleanStart, clampOffset(source, end));
    const token = lineCommentToken(options.language, options.path);
    if (!token) return { value: source, start: cleanStart, end: cleanEnd, handled: false };
    const starts = selectedLineStarts(source, cleanStart, cleanEnd);
    const records = starts.map((at) => {
      const lineEnd = source.indexOf('\n', at);
      const line = source.slice(at, lineEnd === -1 ? source.length : lineEnd);
      const indent = leadingWhitespace(line).length;
      const body = line.slice(indent);
      return { at: at + indent, body, blank: !body.trim() };
    });
    const meaningful = records.filter(record => !record.blank);
    if (!meaningful.length) return { value: source, start: cleanStart, end: cleanEnd, handled: true };
    const removeComments = meaningful.every(record => record.body.startsWith(token));
    const edits = meaningful.map((record) => {
      if (!removeComments) return { at: record.at, remove: 0, insert: `${token} ` };
      const suffix = record.body.slice(token.length);
      return { at: record.at, remove: token.length + (/^[ \t]/.test(suffix) ? 1 : 0), insert: '' };
    });
    return {
      value: applyEdits(source, edits),
      start: mapOffset(cleanStart, edits, cleanStart === cleanEnd),
      end: mapOffset(cleanEnd, edits, true),
      handled: true,
    };
  }

  return Object.freeze({
    activeIndentScope,
    applyEnterEdit,
    applyIndentEdit,
    commandContextAt,
    cursorPosition,
    cursorPositionFromStarts,
    defaultIndentSize,
    detectIndentation,
    explainLine,
    guideColumns,
    indentationColumns,
    inlineCompletionAt,
    lineCommentToken,
    lineStarts,
    normalizeTabSize,
    toggleLineComment,
    visualWidth,
  });
});
