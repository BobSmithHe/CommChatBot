<template>
  <div class="tree-node">
    <div
      :class="['tree-row', { 'drag-target': dragOver }]"
      :style="{ paddingLeft: `${depth * 13 + 4}px` }"
      draggable="true"
      @dragstart="startDrag"
      @dragend="emit('drag-end')"
      @dragover="handleDragOver"
      @dragleave="dragOver = false"
      @drop="handleDrop"
    >
      <button
        :class="['tree-main', { active: node.path === activePath, directory: node.type === 'directory' }]"
        type="button"
        @click="activate"
      >
        <ChevronDown v-if="node.type === 'directory' && expanded" :size="13" />
        <ChevronRight v-else-if="node.type === 'directory'" :size="13" />
        <span v-else class="tree-spacer" />
        <FolderOpen v-if="node.type === 'directory' && expanded" :size="14" />
        <Folder v-else-if="node.type === 'directory'" :size="14" />
        <FileCode2 v-else-if="node.name.endsWith('.py')" :size="14" />
        <FileText v-else :size="14" />
        <span class="tree-name">{{ node.name }}</span>
      </button>
      <div class="tree-actions">
        <button v-if="node.type === 'directory'" type="button" :title="`在 ${node.name} 中新建文件`" @click="emitCreate('file')"><FilePlus2 :size="12" /></button>
        <button v-if="node.type === 'directory'" type="button" :title="`在 ${node.name} 中新建文件夹`" @click="emitCreate('directory')"><FolderPlus :size="12" /></button>
        <button class="tree-delete" type="button" :title="`删除 ${node.name}`" @click="emit('delete', node)"><Trash2 :size="12" /></button>
      </div>
    </div>
    <div v-if="node.type === 'directory' && expanded">
      <WorkspaceTreeNode
        v-for="child in node.children"
        :key="child.path"
        :node="child"
        :depth="depth + 1"
        :active-path="activePath"
        :default-expanded="defaultExpanded"
        @open="emit('open', $event)"
        @delete="emit('delete', $event)"
        @select-directory="emit('select-directory', $event)"
        @create="emit('create', $event)"
        @drag-start="emit('drag-start', $event)"
        @drag-end="emit('drag-end')"
        @drop-entry="emit('drop-entry', $event)"
      />
    </div>
  </div>
</template>

<script setup>
import { ref } from "vue";
import { ChevronDown, ChevronRight, FileCode2, FilePlus2, FileText, Folder, FolderOpen, FolderPlus, Trash2 } from "lucide-vue-next";

const props = defineProps({
  node: { type: Object, required: true },
  depth: { type: Number, default: 0 },
  activePath: { type: String, default: "" },
  defaultExpanded: { type: Boolean, default: true },
});
const emit = defineEmits(["open", "delete", "select-directory", "create", "drag-start", "drag-end", "drop-entry"]);
const expanded = ref(props.defaultExpanded);
const dragOver = ref(false);

function activate() {
  if (props.node.type === "directory") {
    expanded.value = !expanded.value;
    emit("select-directory", props.node.path);
  }
  else emit("open", props.node.path);
}

function emitCreate(kind) {
  emit("create", { kind, directory: props.node.path });
}

function startDrag(event) {
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", props.node.path);
  emit("drag-start", props.node);
}

function handleDragOver(event) {
  if (props.node.type !== "directory") return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  dragOver.value = true;
}

function handleDrop(event) {
  if (props.node.type !== "directory") return;
  event.preventDefault();
  event.stopPropagation();
  dragOver.value = false;
  emit("drop-entry", props.node.path);
}
</script>
