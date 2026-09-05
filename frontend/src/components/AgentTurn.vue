<template>
  <section class="trace-turn agent-turn">
    <button type="button" class="trace-turn-toggle" @click="turn.anchor.turnExpanded = turn.anchor.turnExpanded === false">
      <span>{{ turn.label }}</span>
      <small>{{ stepCount }} 步{{ turn.completed ? " · 已完成" : " · 运行中" }}</small>
      <ChevronDown v-if="turn.anchor.turnExpanded !== false" :size="13" />
      <ChevronRight v-else :size="13" />
    </button>

    <div v-show="turn.anchor.turnExpanded !== false" class="agent-flow">
      <template v-for="block in blocks" :key="block.key">
        <details v-if="block.type === 'thinking'" class="agent-thinking">
          <summary><Sparkles :size="13" /> 思考过程</summary>
          <p>{{ block.text || "思考中…" }}</p>
        </details>

        <article v-else-if="block.type === 'tool'" :class="['agent-tool', { failed: block.result?.is_error }]">
          <header class="agent-tool-header">
            <span class="agent-action-dot">●</span>
            <strong>{{ toolLabel(block.use.name) }}</strong>
            <code>{{ toolSummary(block.use) }}</code>
            <button type="button" @click="toggle(`input-${block.key}`)">
              {{ expanded.has(`input-${block.key}`) ? "收起参数" : "参数" }}
            </button>
          </header>
          <pre v-if="expanded.has(`input-${block.key}`)" class="agent-tool-input">{{ formatJson(block.use.input) }}</pre>

          <div v-if="block.use.name === 'update_plan'" class="agent-plan">
            <label v-for="(step, index) in block.use.input?.steps || []" :key="index">
              <input type="checkbox" :checked="step.status === 'completed'" disabled />
              <span :class="{ active: step.status === 'in_progress' }">{{ step.description }}</span>
            </label>
          </div>

          <div v-if="block.result" class="agent-observation">
            <span class="agent-observation-mark">⎿</span>
            <div class="agent-observation-body">
              <div v-if="resultPayload(block.result).diff" class="agent-diff">
                <div
                  v-for="(line, index) in diffLines(resultPayload(block.result).diff)"
                  :key="index"
                  :class="diffLineClass(line)"
                >{{ line }}</div>
              </div>
              <pre v-if="resultText(block.result)">{{ visibleResult(block) }}</pre>
              <button
                v-if="resultText(block.result).length > outputLimit"
                type="button"
                class="agent-expand-output"
                @click="toggle(`output-${block.key}`)"
              >{{ expanded.has(`output-${block.key}`) ? "收起" : `展开全部（${resultText(block.result).length} 字符）` }}</button>
            </div>
          </div>
          <div v-else class="agent-observation pending"><span>⎿</span> {{ progressLabel(block) }}</div>
        </article>

        <div v-else-if="block.type === 'response'" class="agent-response">
          <span>LLM 响应</span>
          <div>
            <p>{{ visibleResponse(block) }}</p>
            <button
              v-if="block.text.length > outputLimit"
              type="button"
              class="agent-expand-output"
              @click="toggle(`response-${block.key}`)"
            >{{ expanded.has(`response-${block.key}`) ? "收起" : `展开全部（${block.text.length} 字符）` }}</button>
          </div>
        </div>

        <div v-else class="agent-note">
          <span>{{ eventLabel(block.event.event) }}</span>
          <pre>{{ formatEvent(block.event.content) }}</pre>
        </div>
      </template>
      <div v-if="!blocks.length" class="trace-empty">正在等待运行步骤…</div>
    </div>
  </section>
</template>

<script setup>
import { computed, reactive } from "vue";
import { ChevronDown, ChevronRight, Sparkles } from "lucide-vue-next";

const props = defineProps({ turn: { type: Object, required: true } });
const expanded = reactive(new Set());
const outputLimit = 1600;

const blocks = computed(() => {
  const items = [];
  const tools = new Map();
  let thinking = null;
  for (const event of props.turn.events || []) {
    if (event.event === "thinking") {
      if (!thinking) {
        thinking = { type: "thinking", key: `thinking-${event.id}`, text: "" };
        items.push(thinking);
      }
      thinking.text += String(event.content?.delta || event.content || "");
      continue;
    }
    thinking = null;
    if (event.event === "tool_use") {
      const block = { type: "tool", key: `tool-${event.content?.id || event.id}`, use: event.content || {}, result: null, updates: [] };
      tools.set(String(event.content?.id || ""), block);
      items.push(block);
      continue;
    }
    if (event.event === "tool_update") {
      const owner = tools.get(String(event.content?.id || ""));
      if (owner) owner.updates.push(event.content || {});
      else items.push({ type: "note", key: `progress-${event.id}`, event });
      continue;
    }
    if (event.event === "tool_result") {
      const owner = tools.get(String(event.content?.id || ""));
      if (owner) owner.result = event.content;
      else items.push({ type: "note", key: `orphan-${event.id}`, event });
      continue;
    }
    if (event.event === "llm_response") {
      const text = String(event.content?.text || "").trim();
      if (text) items.push({ type: "response", key: `response-${event.id}`, text });
      continue;
    }
    if (event.event !== "turn_start" && event.event !== "turn_end") {
      items.push({ type: "note", key: `note-${event.id}`, event });
    }
  }
  return items;
});

const stepCount = computed(() => blocks.value.filter((item) => item.type !== "thinking").length);

function toggle(key) {
  if (expanded.has(key)) expanded.delete(key);
  else expanded.add(key);
}

function toolLabel(name) {
  const labels = {
    run_command: "Bash", execute_python: "Python", read_file: "Read", list_files: "List",
    search_files: "Search", write_file: "Write", replace_in_file: "Edit", apply_patch: "Patch",
    update_plan: "Plan", delegate_task: "Delegate", get_diagnostics: "Diagnostics", review_changes: "Review",
  };
  return labels[name] || name || "Tool";
}

function toolSummary(use) {
  const input = use.input || {};
  if (Array.isArray(input.argv)) return `(${input.argv.join(" ")})`;
  if (input.path) return `(${input.path})`;
  if (input.query) return `(${String(input.query).slice(0, 90)})`;
  if (input.task) return `(${String(input.task).slice(0, 90)})`;
  return "";
}

function progressLabel(block) {
  const stage = block.updates?.[block.updates.length - 1]?.stage;
  return ({ waiting_approval: "等待批准…", running: "正在执行…" })[stage] || "准备执行…";
}

function formatJson(value) {
  return JSON.stringify(value || {}, null, 2);
}

function parseOutput(result) {
  const output = result?.output;
  if (typeof output !== "string") return output;
  try { return JSON.parse(output); } catch { return output; }
}

function resultPayload(result) {
  const parsed = parseOutput(result);
  return parsed && typeof parsed === "object" ? parsed : {};
}

function resultText(result) {
  const parsed = parseOutput(result);
  if (typeof parsed === "string") return parsed;
  if (!parsed || typeof parsed !== "object") return String(parsed ?? "");
  const parts = [];
  if (parsed.message) parts.push(String(parsed.message));
  if (parsed.stdout) parts.push(String(parsed.stdout));
  if (parsed.stderr) parts.push(String(parsed.stderr));
  if (Array.isArray(parsed.diagnostics) && parsed.diagnostics.length) {
    parts.push(parsed.diagnostics.map((item) => (
      `${item.path || ""}:${item.start_line || 1}:${item.start_column || 1} ${item.code || ""} ${item.message || ""}`
    )).join("\n"));
  }
  if (!parts.length) {
    const compact = { ...parsed };
    delete compact.diff;
    return JSON.stringify(compact, null, 2);
  }
  return parts.join("\n").trim();
}

function visibleResult(block) {
  const text = resultText(block.result);
  return expanded.has(`output-${block.key}`) || text.length <= outputLimit
    ? text
    : `${text.slice(0, outputLimit)}\n…（已隐藏 ${text.length - outputLimit} 字符）`;
}

function visibleResponse(block) {
  return expanded.has(`response-${block.key}`) || block.text.length <= outputLimit
    ? block.text
    : `${block.text.slice(0, outputLimit)}\n…（已隐藏 ${block.text.length - outputLimit} 字符）`;
}

function diffLines(diff) {
  return String(diff || "").split(/\r?\n/);
}

function diffLineClass(line) {
  if (line.startsWith("+") && !line.startsWith("+++")) return "added";
  if (line.startsWith("-") && !line.startsWith("---")) return "removed";
  if (line.startsWith("@@")) return "hunk";
  return "context";
}

function formatEvent(content) {
  return typeof content === "object" ? JSON.stringify(content, null, 2) : String(content ?? "");
}

function eventLabel(type) {
  return ({ status: "状态", sources: "来源", approval_required: "审批", hook: "Hook", error: "错误", result: "结果", queued_message: "追加指令", context_compacted: "上下文压缩", memory_updated: "记忆已更新", memory_recalled: "已召回记忆" })[type] || type;
}
</script>
