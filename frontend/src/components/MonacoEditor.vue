<template>
  <div ref="container" class="monaco-host" />
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as monaco from "monaco-editor/editor/editor.api.js";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/language/json/json.worker?worker";

self.MonacoEnvironment = {
  getWorker(_moduleId, label) {
    return label === "json" ? new JsonWorker() : new EditorWorker();
  },
};

const props = defineProps({
  modelValue: { type: String, default: "" },
  path: { type: String, default: "untitled.txt" },
});
const emit = defineEmits(["update:modelValue", "save"]);
const container = ref(null);
let editor;
let applyingExternalValue = false;

onMounted(() => {
  editor = monaco.editor.create(container.value, {
    value: props.modelValue,
    language: languageForPath(props.path),
    theme: "vs-dark",
    automaticLayout: true,
    minimap: { enabled: true },
    fontSize: 13,
    lineHeight: 21,
    wordWrap: "off",
    scrollBeyondLastLine: false,
    tabSize: 2,
    insertSpaces: true,
  });
  editor.onDidChangeModelContent(() => {
    if (!applyingExternalValue) emit("update:modelValue", editor.getValue());
  });
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => emit("save"));
});

watch(() => props.modelValue, (value) => {
  if (!editor || editor.getValue() === value) return;
  applyingExternalValue = true;
  editor.setValue(value);
  applyingExternalValue = false;
});

watch(() => props.path, (path) => {
  const model = editor?.getModel();
  if (model) monaco.editor.setModelLanguage(model, languageForPath(path));
});

onBeforeUnmount(() => editor?.dispose());

function languageForPath(path) {
  const extension = String(path).split(".").pop()?.toLowerCase();
  return {
    py: "python", js: "javascript", ts: "typescript", vue: "html", html: "html",
    css: "css", json: "json", md: "markdown", yml: "yaml", yaml: "yaml",
    sh: "shell", ps1: "powershell", sql: "sql",
  }[extension] || "plaintext";
}
</script>

<style scoped>
.monaco-host {
  min-width: 0;
  min-height: 0;
  width: 100%;
  height: 100%;
}
</style>
