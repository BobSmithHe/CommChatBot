<template>
  <div class="app-shell" :style="{ gridTemplateColumns: `${sidebarWidth}px 1px minmax(0, 1fr)` }">
    <aside class="sidebar">
      <div class="brand-row">
        <div class="brand-mark">C</div>
        <div>
          <div class="brand-title">CommChatBot</div>
          <div class="brand-subtitle">{{ healthText }}</div>
        </div>
      </div>

      <button class="primary-action" type="button" :disabled="showArchived" @click="newConversation">
        <Plus :size="16" />
        <span>新会话</span>
      </button>

      <div class="section-label sidebar-section-row">
        <span>{{ showArchived ? "已归档" : (mode === "coding-agent" ? "Coding 会话" : "Chat 会话") }}</span>
        <button type="button" :title="showArchived ? '返回会话' : '查看归档'" @click="toggleArchived"><ArchiveRestore :size="14" /></button>
      </div>
      <div class="conversation-list">
        <button
          v-for="item in conversations"
          :key="item.id"
          :class="['conversation-item', { active: item.id === currentConversationId }]"
          type="button"
          @click="switchConversation(item.id)"
        >
          <span :title="item.title">{{ item.title }}</span>
          <ArchiveRestore v-if="showArchived" :size="14" @click.stop="restoreConversation(item.id)" />
          <Trash2 v-else :size="14" @click.stop="deleteConversation(item.id)" />
        </button>
      </div>
      <div class="account-card">
        <div>
          <UserRound :size="15" />
          <span>{{ currentUser?.username || "未登录" }}</span>
        </div>
        <button v-if="currentUser?.username !== 'anonymous'" type="button" title="退出登录" @click="logout">
          <LogOut :size="14" />
        </button>
        <button v-else type="button" title="登录或注册" @click="openAuth('login')">
          <LogIn :size="14" />
        </button>
      </div>
    </aside>

    <div class="pane-resizer sidebar-resizer" title="拖动调整会话栏宽度" @pointerdown="startResize('sidebar', $event)" />

    <main class="workspace">
      <header class="topbar">
        <div class="title-block">
          <div class="eyebrow">AgentRuntime</div>
          <h1>{{ mode === "coding-agent" ? "Coding Agent" : "Chatbot" }}</h1>
        </div>
        <div class="segmented" aria-label="chat mode">
          <button :class="{ active: mode === 'chatbot' }" type="button" :disabled="isStreaming" @click="mode = 'chatbot'">Chat</button>
          <button :class="{ active: mode === 'coding-agent' }" type="button" :disabled="isStreaming" @click="mode = 'coding-agent'">Coding</button>
        </div>
        <label v-if="mode === 'coding-agent'" class="permission-select" title="Agent 工具权限">
          <span>权限</span>
          <select v-model="permissionMode" :disabled="isStreaming" @change="savePermissionMode">
            <option value="read-only">只读</option>
            <option value="workspace-write">工作区写入</option>
            <option value="full-access">完全访问</option>
          </select>
        </label>
      </header>

      <section
        ref="codingLayoutEl"
        :class="['chat-grid', { 'coding-workspace-layout': mode === 'coding-agent' }]"
        :style="mode === 'coding-agent' ? { gridTemplateColumns: `${codingPaneWidth}px 1px minmax(0, 1fr)` } : undefined"
      >
        <div ref="transcriptPanelEl" class="transcript-panel">
          <div ref="messagesEl" class="messages">
            <div v-if="messages.length === 0" class="empty-state">
              <Sparkles :size="28" />
              <h2>{{ mode === "coding-agent" ? "开始一个编码任务" : "开始一次对话" }}</h2>
              <p>{{ mode === "coding-agent" ? "Agent 与右侧编辑器共享本会话的独立工作区。" : "可在输入框按需启用 RAG、网络搜索或上传文件。" }}</p>
            </div>

            <article v-for="message in messages" :key="message.localId" :class="['message', message.role]">
              <div class="message-avatar">
                <UserRound v-if="message.role === 'user'" :size="16" />
                <Bot v-else :size="16" />
              </div>
              <div class="message-body">
                <div class="message-meta">{{ message.role === 'user' ? 'You' : 'CommChatBot' }}</div>
                <div v-if="message.role === 'assistant'" class="message-trace">
                  <button type="button" class="trace-toggle" @click="message.traceExpanded = !message.traceExpanded">
                    <Activity :size="14" />
                    <span>运行轨迹 · {{ message.events?.length ? `${message.events.length} 步` : "无历史记录" }}</span>
                    <ChevronDown v-if="message.traceExpanded" :size="14" />
                    <ChevronRight v-else :size="14" />
                  </button>
                  <div v-show="message.traceExpanded" class="trace-events">
                    <div v-for="event in message.events" :key="event.id" :class="['trace-event', event.event]">
                      <span>{{ event.event }}</span>
                      <p>{{ formatEventContent(event.content) }}</p>
                      <div v-if="event.event === 'approval_required' && event.approvalPending" class="approval-actions">
                        <button type="button" @click="resolveApproval(event, true)">允许</button>
                        <button type="button" class="reject" @click="resolveApproval(event, false)">拒绝</button>
                      </div>
                    </div>
                    <div v-if="!message.events?.length" class="trace-empty">
                      {{ isActiveAssistant(message) ? "正在等待运行步骤…" : "该回答生成于轨迹记录启用前。" }}
                    </div>
                  </div>
                </div>
                <div class="markdown" v-html="renderMessageMarkdown(message)" @click="handleMarkdownAction" />
              </div>
            </article>
          </div>

          <form class="composer" @submit.prevent="sendMessage">
            <div v-if="attachedFiles.length" class="attachment-list">
              <div v-for="file in attachedFiles" :key="file.id" class="attachment-chip">
                <FileText :size="14" />
                <span>{{ file.name }}</span>
                <button type="button" aria-label="移除附件" @click="removeAttachment(file.id)"><X :size="13" /></button>
              </div>
            </div>
            <textarea
              v-model="draft"
              :disabled="isStreaming || attachmentUploading"
              rows="3"
              placeholder="输入无线通信问题、仿真需求或代码任务..."
              @keydown.enter.exact.prevent="sendMessage"
            />
            <div class="composer-footer">
              <div class="composer-left">
                <div v-if="mode === 'chatbot'" class="composer-tools">
                  <label class="composer-tool file-tool" :class="{ disabled: isStreaming || attachmentUploading }" title="上传附件">
                    <Paperclip :size="15" />
                    <span>{{ attachmentUploading ? "上传中" : "附件" }}</span>
                    <input type="file" multiple accept=".txt,.md,.pdf,.py,.json,.csv" :disabled="isStreaming || attachmentUploading" @change="uploadChatAttachments" />
                  </label>
                  <label :class="['composer-tool', { active: useRag }]" title="使用本地知识库">
                    <input v-model="useRag" type="checkbox" />
                    <Database :size="15" /><span>RAG</span>
                  </label>
                  <label :class="['composer-tool', { active: useWeb }]" title="搜索网页">
                    <input v-model="useWeb" type="checkbox" />
                    <Globe2 :size="15" /><span>网络</span>
                  </label>
                </div>
                <button v-if="recoverableTask && !isStreaming" class="resume-task" type="button" @click="resumeLastTask">恢复中断任务</button>
                <div :class="['composer-status', { error: attachmentError }]">{{ attachmentError || composerStatus }}</div>
              </div>
              <button v-if="isStreaming" class="send-button stop" type="button" @click="cancelCurrentTask">
                <Square :size="14" /><span>停止</span>
              </button>
              <button v-else class="send-button" type="submit" :disabled="attachmentUploading || (!draft.trim() && attachedFiles.length === 0)">
                <SendHorizontal :size="16" /><span>发送</span>
              </button>
            </div>
          </form>

          <section v-if="mode === 'coding-agent'" class="terminal-panel" :style="{ height: `${terminalHeight}px` }">
            <div class="terminal-resizer" title="拖动调整终端高度" @pointerdown="startResize('terminal', $event)" />
            <div class="terminal-toolbar">
              <div class="terminal-tabs">
                <div v-for="terminal in terminals" :key="terminal.id" :class="['terminal-tab', { active: terminal.id === activeTerminalId }]">
                  <button type="button" @click="activeTerminalId = terminal.id">{{ terminal.title }}</button>
                  <button type="button" :aria-label="`关闭 ${terminal.title}`" @click="closeTerminal(terminal.id)"><X :size="12" /></button>
                </div>
              </div>
              <button class="terminal-new" type="button" title="新建终端" @click="createTerminal"><Plus :size="14" /></button>
              <button v-if="terminalRunning" class="terminal-new danger" type="button" title="中断当前命令" @click="interruptTerminal"><Square :size="12" /></button>
            </div>
            <pre ref="terminalEl" class="terminal-output">{{ activeTerminal?.output || "点击 + 新建终端" }}</pre>
            <form class="terminal-input-row" @submit.prevent="executeTerminalCommand">
              <span>PS {{ activeTerminal?.cwd || "." }}&gt;</span>
              <input v-model="terminalCommand" :disabled="!activeTerminal || terminalRunning" autocomplete="off" spellcheck="false" aria-label="终端命令" />
              <button type="submit" aria-label="执行终端命令" :disabled="!activeTerminal || terminalRunning || !terminalCommand.trim()"><Play :size="13" /></button>
            </form>
          </section>
        </div>

        <div v-if="mode === 'coding-agent'" class="pane-resizer coding-resizer" title="拖动调整对话与编辑器宽度" @pointerdown="startResize('coding', $event)" />

        <aside v-if="mode === 'coding-agent'" class="workspace-editor">
          <div class="editor-header">
            <div><strong>EXPLORER</strong><span>{{ workspaceId ? workspaceId.slice(0, 8) : "创建中" }}</span></div>
            <div class="explorer-actions">
              <button type="button" title="新建文件" @click="beginWorkspaceCreate('file')"><FilePlus2 :size="15" /></button>
              <button type="button" title="新建文件夹" @click="beginWorkspaceCreate('directory')"><FolderPlus :size="15" /></button>
              <button type="button" title="刷新" :disabled="workspaceLoading" @click="refreshWorkspace"><RefreshCw :size="15" /></button>
              <button type="button" title="查看更改" @click="openWorkspaceChanges"><GitCompare :size="15" /></button>
              <button type="button" title="创建 Git 检查点" @click="checkpointWorkspace"><ShieldCheck :size="15" /></button>
              <button type="button" title="折叠全部" @click="collapseWorkspaceTree"><ChevronsUp :size="15" /></button>
            </div>
          </div>
          <div class="editor-layout" :style="{ gridTemplateColumns: `${fileTreeWidth}px 1px minmax(0, 1fr)` }">
            <div class="file-explorer" @dragover.prevent @drop="dropWorkspaceAtRoot">
              <form v-if="workspaceCreateMode" class="explorer-create-row" @submit.prevent="submitWorkspaceCreate">
                <FilePlus2 v-if="workspaceCreateMode === 'file'" :size="14" />
                <FolderPlus v-else :size="14" />
                <input
                  ref="workspaceCreateInputEl"
                  v-model="newWorkspacePath"
                  :placeholder="workspaceCreateBase ? `${workspaceCreateBase}/名称` : (workspaceCreateMode === 'file' ? '文件名' : '文件夹名')"
                  @keydown.esc="cancelWorkspaceCreate"
                />
                <button type="button" title="取消" @click="cancelWorkspaceCreate"><X :size="13" /></button>
              </form>
              <WorkspaceTreeNode
                v-for="node in workspaceTree"
                :key="`${treeVersion}-${node.path}`"
                :node="node"
                :active-path="selectedWorkspaceDirectory || workspaceFilePath"
                :default-expanded="treeDefaultExpanded"
                @open="openWorkspaceFile"
                @delete="deleteWorkspaceEntry"
                @select-directory="selectedWorkspaceDirectory = $event"
                @create="beginWorkspaceCreate($event.kind, $event.directory)"
                @drag-start="draggingWorkspaceEntry = $event"
                @drag-end="draggingWorkspaceEntry = null"
                @drop-entry="moveWorkspaceEntry"
              />
              <div v-if="!workspaceTree.length && !workspaceLoading" class="workspace-empty">空工作区。使用 EXPLORER 工具栏创建文件或文件夹。</div>
              <div v-if="gitStatus.changes?.length" class="scm-section">
                <strong>CHANGES · {{ gitStatus.branch }}</strong>
                <button v-for="change in gitStatus.changes" :key="`${change.status}-${change.path}`" type="button" @click="openWorkspaceDiff(change.path)">
                  <span>{{ change.status }}</span><em>{{ change.path }}</em>
                </button>
              </div>
              <div v-if="trashItems.length" class="scm-section trash-section">
                <strong>RECYCLE BIN</strong>
                <div v-for="item in trashItems" :key="item.id" class="trash-item">
                  <span :title="item.path">{{ item.path }}</span>
                  <button type="button" title="恢复" @click="restoreTrashItem(item.id)"><RotateCcw :size="12" /></button>
                </div>
              </div>
            </div>
            <div class="pane-resizer file-tree-resizer" title="拖动调整文件树宽度" @pointerdown="startResize('files', $event)" />
            <div class="editor-pane">
              <div class="file-tabbar">
                <span>{{ workspaceFilePath || "未打开文件" }}<i v-if="workspaceDirty"> ●</i></span>
                <div class="file-actions">
                  <button type="button" title="运行当前 Python 文件" :disabled="!canRunWorkspaceFile || workspaceRunning" @click="runWorkspaceFile"><Play :size="15" /></button>
                  <button type="button" title="保存" :disabled="!workspaceFilePath || !workspaceDirty || workspaceSaving" @click="saveWorkspaceFile"><Save :size="15" /></button>
                </div>
              </div>
              <div v-if="reviewVisible" class="diff-review">
                <div class="diff-review-toolbar">
                  <strong>DIFF · {{ reviewPath || '全部更改' }}</strong>
                  <div>
                    <button v-if="reviewPath" type="button" @click="restoreGitFile(reviewPath)"><RotateCcw :size="13" />撤销文件</button>
                    <button type="button" @click="reviewVisible = false"><X :size="13" /></button>
                  </div>
                </div>
                <pre>{{ gitDiff || '没有可显示的差异。' }}</pre>
              </div>
              <MonacoEditor v-else-if="workspaceFilePath" v-model="workspaceContent" :path="workspaceFilePath" @save="saveWorkspaceFile" />
              <div v-else class="editor-empty">从文件列表打开文件</div>
              <div v-if="workspaceOutput" class="workspace-output">
                <div><strong>运行输出</strong><button type="button" @click="workspaceOutput = ''"><X :size="13" /></button></div>
                <pre>{{ workspaceOutput }}</pre>
              </div>
            </div>
          </div>
        </aside>
      </section>
    </main>
  </div>

  <div v-if="authOpen" class="auth-overlay" @click.self="closeAuth">
    <form class="auth-dialog" @submit.prevent="submitAuth">
      <div class="auth-heading">
        <div>
          <span>CommChatBot</span>
          <h2>{{ authMode === 'login' ? '登录' : '创建账户' }}</h2>
        </div>
        <button v-if="currentUser" type="button" title="关闭" @click="closeAuth"><X :size="16" /></button>
      </div>
      <label>
        <span>用户名</span>
        <input v-model.trim="authForm.username" name="username" autocomplete="username" minlength="2" required />
      </label>
      <label v-if="authMode === 'register'">
        <span>邮箱</span>
        <input v-model.trim="authForm.email" name="email" type="email" autocomplete="email" required />
      </label>
      <label>
        <span>密码</span>
        <input v-model="authForm.password" name="password" type="password" :autocomplete="authMode === 'login' ? 'current-password' : 'new-password'" minlength="6" required />
      </label>
      <p v-if="authError" class="auth-error">{{ authError }}</p>
      <button class="auth-submit" type="submit" :disabled="authSubmitting">
        {{ authSubmitting ? '处理中…' : (authMode === 'login' ? '登录' : '注册并登录') }}
      </button>
      <button class="auth-switch" type="button" @click="switchAuthMode">
        {{ authMode === 'login' ? '没有账户？创建一个' : '已有账户？返回登录' }}
      </button>
    </form>
  </div>
</template>

<script setup>
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import {
  Activity, ArchiveRestore, Bot, ChevronDown, ChevronRight, ChevronsUp, Database, FilePlus2, FileText,
  FolderPlus, GitCompare, Globe2, LogIn, LogOut, Paperclip, Play, Plus, RefreshCw, RotateCcw, Save,
  SendHorizontal, ShieldCheck, Sparkles, Square, Trash2, UserRound, X,
} from "lucide-vue-next";
import { apiDelete, apiGet, apiPatch, apiPost, setAuthToken, streamChat, streamTerminal, uploadFile } from "./api/client";
import WorkspaceTreeNode from "./components/WorkspaceTreeNode.vue";
import { renderMarkdown } from "./utils/markdown";

const MonacoEditor = defineAsyncComponent(() => import("./components/MonacoEditor.vue"));

const mode = ref("chatbot");
const useRag = ref(true);
const useWeb = ref(false);
const healthText = ref("offline");
const conversations = ref([]);
const showArchived = ref(false);
const currentConversationId = ref(null);
const permissionMode = ref("workspace-write");
const currentTaskId = ref("");
const recoverableTask = ref(null);
const pendingResumeTaskId = ref("");
const currentUser = ref(null);
const authOpen = ref(false);
const authMode = ref("login");
const authSubmitting = ref(false);
const authError = ref("");
const authForm = ref({ username: "", email: "", password: "" });
const messages = ref([]);
const messagesEl = ref(null);
const terminalEl = ref(null);
const codingLayoutEl = ref(null);
const transcriptPanelEl = ref(null);
const workspaceCreateInputEl = ref(null);
const draft = ref("");
const isStreaming = ref(false);
const attachedFiles = ref([]);
const attachmentUploading = ref(false);
const attachmentError = ref("");
const workspaceId = ref("");
const workspaceEntries = ref([]);
const workspaceFilePath = ref("");
const selectedWorkspaceDirectory = ref("");
const workspaceContent = ref("");
const workspaceOriginal = ref("");
const workspaceLoading = ref(false);
const workspaceSaving = ref(false);
const workspaceRunning = ref(false);
const workspaceOutput = ref("");
const gitStatus = ref({ is_repo: false, branch: null, changes: [] });
const gitDiff = ref("");
const reviewVisible = ref(false);
const reviewPath = ref("");
const trashItems = ref([]);
const newWorkspacePath = ref("");
const workspaceCreateMode = ref("");
const workspaceCreateBase = ref("");
const draggingWorkspaceEntry = ref(null);
const treeVersion = ref(0);
const treeDefaultExpanded = ref(true);
const terminals = ref([]);
const activeTerminalId = ref("");
const terminalCommand = ref("");
const terminalRunning = ref(false);
const terminalCreating = ref(false);
const sidebarWidth = ref(280);
const codingPaneWidth = ref(500);
const fileTreeWidth = ref(220);
const terminalHeight = ref(248);
let resizeCleanup = null;
let localId = 0;
let eventId = 0;
const lastConversationIds = { chatbot: null, "coding-agent": null };

const workspaceDirty = computed(() => workspaceContent.value !== workspaceOriginal.value);
const canRunWorkspaceFile = computed(() => workspaceFilePath.value.toLowerCase().endsWith(".py"));
const activeTerminal = computed(() => terminals.value.find((terminal) => terminal.id === activeTerminalId.value) || null);
const workspaceTree = computed(() => buildWorkspaceTree(workspaceEntries.value));
const composerStatus = computed(() => {
  if (isStreaming.value) return "running";
  if (mode.value === "coding-agent") return "coding-agent · 独立工作区";
  return `${useRag.value ? "rag" : "no-rag"} · ${useWeb.value ? "web" : "local"}`;
});

onMounted(async () => {
  await refreshHealth();
  if (await loadCurrentUser()) await loadConversations(true);
});

onBeforeUnmount(() => resizeCleanup?.());

watch(mode, async () => {
  showArchived.value = false;
  currentConversationId.value = null;
  messages.value = [];
  attachedFiles.value = [];
  attachmentError.value = "";
  clearWorkspaceState();
  await loadConversations(true);
});

async function refreshHealth() {
  try {
    const health = await apiGet("/health");
    healthText.value = `${health.status} · ${health.version}`;
  } catch {
    healthText.value = "offline";
  }
}

async function loadCurrentUser() {
  try {
    currentUser.value = await apiGet("/api/auth/me");
    return true;
  } catch {
    setAuthToken("");
    try {
      currentUser.value = await apiGet("/api/auth/me");
      return true;
    } catch {
      currentUser.value = null;
      authOpen.value = true;
      return false;
    }
  }
}

function openAuth(nextMode = "login") {
  authMode.value = nextMode;
  authError.value = "";
  authForm.value = { username: "", email: "", password: "" };
  authOpen.value = true;
}

function closeAuth() {
  if (currentUser.value) authOpen.value = false;
}

function switchAuthMode() {
  authMode.value = authMode.value === "login" ? "register" : "login";
  authError.value = "";
}

async function submitAuth() {
  if (authSubmitting.value) return;
  authSubmitting.value = true;
  authError.value = "";
  try {
    if (authMode.value === "register") {
      await apiPost("/api/auth/register", {
        username: authForm.value.username,
        email: authForm.value.email,
        password: authForm.value.password,
      });
    }
    const session = await apiPost("/api/auth/login", {
      username: authForm.value.username,
      password: authForm.value.password,
    });
    setAuthToken(session.access_token);
    currentUser.value = await apiGet("/api/auth/me");
    authOpen.value = false;
    await reloadIdentityScope();
  } catch (error) {
    authError.value = friendlyApiError(error);
  } finally {
    authSubmitting.value = false;
  }
}

async function logout() {
  if (isStreaming.value) return;
  setAuthToken("");
  if (await loadCurrentUser()) await reloadIdentityScope();
}

async function reloadIdentityScope() {
  conversations.value = [];
  currentConversationId.value = null;
  messages.value = [];
  attachedFiles.value = [];
  showArchived.value = false;
  lastConversationIds.chatbot = null;
  lastConversationIds["coding-agent"] = null;
  clearWorkspaceState();
  await loadConversations(true);
}

function friendlyApiError(error) {
  const text = String(error?.message || error || "请求失败");
  try {
    const parsed = JSON.parse(text);
    return parsed.detail || text;
  } catch {
    return text;
  }
}

async function loadConversations(openConversation = false) {
  const requestedMode = mode.value;
  const loaded = await apiGet(`/api/conversations?mode=${encodeURIComponent(requestedMode)}&archived=${showArchived.value}`);
  if (mode.value !== requestedMode) return;
  conversations.value = loaded;
  if (!openConversation) return;
  const selected = loaded.find((item) => item.id === lastConversationIds[requestedMode]) || loaded[0];
  if (selected) await switchConversation(selected.id);
  else if (!showArchived.value) await newConversation();
}

async function newConversation() {
  const requestedMode = mode.value;
  const conv = await apiPost("/api/conversations", { mode: requestedMode });
  if (mode.value !== requestedMode) return;
  conversations.value = [conv, ...conversations.value];
  currentConversationId.value = conv.id;
  permissionMode.value = conv.permission_mode || "workspace-write";
  lastConversationIds[requestedMode] = conv.id;
  messages.value = [];
  attachedFiles.value = [];
  attachmentError.value = "";
  clearWorkspaceState();
  if (requestedMode === "coding-agent") {
    await refreshWorkspace();
    await loadTerminals();
  }
}

async function switchConversation(id) {
  const data = await apiGet(`/api/conversations/${id}`);
  if (data.mode !== mode.value) return;
  currentConversationId.value = id;
  permissionMode.value = data.permission_mode || "workspace-write";
  lastConversationIds[mode.value] = id;
  messages.value = data.messages
    .filter((item) => item.role === "user" || item.role === "assistant")
    .map((item) => ({
      ...item,
      localId: ++localId,
      events: (item.trace || []).map((event) => ({ ...event, id: ++eventId })),
      traceExpanded: false,
    }));
  attachedFiles.value = [];
  attachmentError.value = "";
  clearWorkspaceState();
  if (mode.value === "coding-agent") {
    await refreshWorkspace();
    await loadTerminals();
  }
  await loadRecoverableTask();
  await scrollMessages();
}

async function deleteConversation(id) {
  await apiDelete(`/api/conversations/${id}`);
  conversations.value = conversations.value.filter((item) => item.id !== id);
  if (currentConversationId.value === id) {
    if (conversations.value.length) await switchConversation(conversations.value[0].id);
    else await newConversation();
  }
}

async function toggleArchived() {
  showArchived.value = !showArchived.value;
  currentConversationId.value = null;
  messages.value = [];
  await loadConversations(true);
}

async function restoreConversation(id) {
  await apiPost(`/api/conversations/${id}/restore`, {});
  conversations.value = conversations.value.filter((item) => item.id !== id);
}

async function savePermissionMode() {
  if (!currentConversationId.value) return;
  await apiPatch(`/api/conversations/${currentConversationId.value}/permission`, { permission_mode: permissionMode.value });
}

async function sendMessage() {
  const resumeTaskId = pendingResumeTaskId.value;
  const isResume = Boolean(resumeTaskId);
  const attachmentSnapshot = [...attachedFiles.value];
  const content = draft.value.trim() || (attachmentSnapshot.length ? "请分析附件。" : "");
  if (!content || isStreaming.value) return;
  if (!currentConversationId.value) await newConversation();

  draft.value = "";
  attachedFiles.value = [];
  attachmentError.value = "";
  const attachmentLine = attachmentSnapshot.length ? `\n\n📎 ${attachmentSnapshot.map((item) => item.name).join(", ")}` : "";
  if (!isResume) messages.value.push({ localId: ++localId, role: "user", content: content + attachmentLine });
  messages.value.push({ localId: ++localId, role: "assistant", content: "", events: [], traceExpanded: true });
  const assistant = messages.value[messages.value.length - 1];
  isStreaming.value = true;
  await scrollMessages();

  try {
    await streamChat(
      {
        message: content,
        conversation_id: currentConversationId.value,
        mode: mode.value,
        use_rag: mode.value === "chatbot" && useRag.value,
        use_web: mode.value === "chatbot" && useWeb.value,
        attachment_ids: !isResume && mode.value === "chatbot" ? attachmentSnapshot.map((item) => item.id) : [],
        resume_task_id: resumeTaskId || null,
      },
      async (event) => {
        if (event.event === "task") currentTaskId.value = event.content?.task_id || "";
        else if (event.event === "answer") assistant.content += event.content || "";
        else if (event.event === "cancelled") assistant.content += "\n\n_任务已停止。_";
        else if (
          ["status", "result", "sources", "approval_required", "hook"].includes(event.event)
          && event.content
          && !String(event.content).startsWith("Connected.")
        ) assistant.events.push({
          ...event,
          id: ++eventId,
          approvalPending: event.event === "approval_required",
        });
        await scrollMessages();
      },
    );
    await loadConversations();
  } catch (error) {
    assistant.content = `请求失败：${error.message || error}`;
  } finally {
    assistant.traceExpanded = false;
    isStreaming.value = false;
    currentTaskId.value = "";
    pendingResumeTaskId.value = "";
    if (mode.value === "coding-agent") await refreshWorkspace();
    await scrollMessages();
  }
}

async function cancelCurrentTask() {
  if (!currentTaskId.value) return;
  await apiPost(`/api/tasks/${currentTaskId.value}/cancel`, {});
}

async function resolveApproval(event, approved) {
  const content = event.content || {};
  if (!content.task_id || !content.approval_id || !event.approvalPending) return;
  await apiPost(`/api/tasks/${content.task_id}/approvals/${content.approval_id}`, { approved });
  event.approvalPending = false;
  event.approvalDecision = approved ? "approved" : "rejected";
}

function formatEventContent(content) {
  if (content && typeof content === "object") return JSON.stringify(content, null, 2);
  return String(content ?? "");
}

async function loadRecoverableTask() {
  recoverableTask.value = null;
  if (!currentConversationId.value) return;
  const tasks = await apiGet(`/api/tasks?conversation_id=${currentConversationId.value}`);
  recoverableTask.value = tasks.find(
    (task) => task.has_checkpoint && ["failed", "cancelled", "interrupted"].includes(task.status),
  ) || null;
}

async function resumeLastTask() {
  if (!recoverableTask.value) return;
  const data = await apiPost(`/api/tasks/${recoverableTask.value.id}/resume`, {});
  draft.value = data.message || "";
  pendingResumeTaskId.value = data.task_id;
  recoverableTask.value = null;
  await sendMessage();
}

function renderMessageMarkdown(message) {
  return renderMarkdown(message.content, { enableRun: mode.value === "chatbot" && message.role === "assistant" });
}

function isActiveAssistant(message) {
  return isStreaming.value && messages.value.at(-1)?.localId === message.localId;
}

async function handleMarkdownAction(event) {
  const button = event.target.closest?.("[data-run-code]");
  if (!button || mode.value !== "chatbot") return;
  const wrapper = button.closest(".runnable-code");
  const code = wrapper?.querySelector("code")?.innerText || "";
  const output = wrapper?.querySelector(".inline-code-output");
  if (!code || !output) return;
  button.disabled = true;
  button.textContent = "运行中…";
  output.hidden = false;
  output.textContent = "正在执行…";
  try {
    const result = await apiPost("/api/code/execute", { code, language: "python" });
    output.textContent = [result.stdout, result.stderr].filter(Boolean).join("\n") || `执行完成，退出码 ${result.exit_code}`;
  } catch (error) {
    output.textContent = `运行失败：${error.message || error}`;
  } finally {
    button.disabled = false;
    button.textContent = "运行";
  }
}

async function uploadChatAttachments(event) {
  const files = Array.from(event.target.files || []);
  event.target.value = "";
  if (!files.length) return;
  attachmentError.value = "";
  const available = Math.max(0, 5 - attachedFiles.value.length);
  if (!available) {
    attachmentError.value = "最多上传 5 个附件";
    return;
  }
  attachmentUploading.value = true;
  try {
    for (const file of files.slice(0, available)) attachedFiles.value.push(await uploadFile("/api/chat/attachments", file));
    if (files.length > available) attachmentError.value = "最多上传 5 个附件";
  } catch (error) {
    attachmentError.value = `附件上传失败：${error.message || error}`;
  } finally {
    attachmentUploading.value = false;
  }
}

function removeAttachment(id) {
  attachedFiles.value = attachedFiles.value.filter((item) => item.id !== id);
  attachmentError.value = "";
}

function clearWorkspaceState() {
  recoverableTask.value = null;
  pendingResumeTaskId.value = "";
  currentTaskId.value = "";
  workspaceId.value = "";
  workspaceEntries.value = [];
  workspaceFilePath.value = "";
  selectedWorkspaceDirectory.value = "";
  workspaceContent.value = "";
  workspaceOriginal.value = "";
  newWorkspacePath.value = "";
  workspaceCreateMode.value = "";
  workspaceCreateBase.value = "";
  draggingWorkspaceEntry.value = null;
  workspaceOutput.value = "";
  gitStatus.value = { is_repo: false, branch: null, changes: [] };
  gitDiff.value = "";
  reviewVisible.value = false;
  reviewPath.value = "";
  trashItems.value = [];
  terminals.value = [];
  activeTerminalId.value = "";
  terminalCommand.value = "";
}

function beginWorkspaceCreate(kind, directory = selectedWorkspaceDirectory.value) {
  workspaceCreateMode.value = kind;
  workspaceCreateBase.value = directory || "";
  selectedWorkspaceDirectory.value = directory || "";
  newWorkspacePath.value = "";
  nextTick(() => workspaceCreateInputEl.value?.focus());
}

function cancelWorkspaceCreate() {
  workspaceCreateMode.value = "";
  workspaceCreateBase.value = "";
  newWorkspacePath.value = "";
}

async function submitWorkspaceCreate() {
  if (workspaceCreateMode.value === "directory") await createWorkspaceDirectory();
  else await createWorkspaceFile();
}

function collapseWorkspaceTree() {
  treeDefaultExpanded.value = false;
  treeVersion.value += 1;
}

async function refreshWorkspace() {
  if (mode.value !== "coding-agent" || !currentConversationId.value) return;
  workspaceLoading.value = true;
  try {
    const data = await apiGet(`/api/conversations/${currentConversationId.value}/workspace/files`);
    workspaceId.value = data.workspace_id || "";
    workspaceEntries.value = data.entries || (data.files || []).map((path) => ({ path, type: "file" }));
    const filePaths = workspaceEntries.value.filter((entry) => entry.type === "file").map((entry) => entry.path);
    if (workspaceFilePath.value && !filePaths.includes(workspaceFilePath.value)) {
      workspaceFilePath.value = "";
      workspaceContent.value = "";
      workspaceOriginal.value = "";
    }
    await Promise.all([refreshGitStatus(), refreshTrash()]);
  } finally {
    workspaceLoading.value = false;
  }
}

async function openWorkspaceFile(path) {
  if (!currentConversationId.value) return;
  const data = await apiGet(`/api/conversations/${currentConversationId.value}/workspace/file?path=${encodeURIComponent(path)}`);
  workspaceFilePath.value = data.path;
  selectedWorkspaceDirectory.value = "";
  workspaceContent.value = data.content;
  workspaceOriginal.value = data.content;
  workspaceOutput.value = "";
  reviewVisible.value = false;
}

async function refreshGitStatus() {
  if (!currentConversationId.value || mode.value !== "coding-agent") return;
  gitStatus.value = await apiGet(`/api/conversations/${currentConversationId.value}/workspace/git/status`);
}

async function refreshTrash() {
  if (!currentConversationId.value || mode.value !== "coding-agent") return;
  const data = await apiGet(`/api/conversations/${currentConversationId.value}/workspace/trash`);
  trashItems.value = data.items || [];
}

async function openWorkspaceChanges() {
  await openWorkspaceDiff("");
}

async function openWorkspaceDiff(path) {
  const suffix = path ? `?path=${encodeURIComponent(path)}` : "";
  const data = await apiGet(`/api/conversations/${currentConversationId.value}/workspace/git/diff${suffix}`);
  gitDiff.value = data.diff || "";
  reviewPath.value = path;
  reviewVisible.value = true;
}

async function checkpointWorkspace() {
  const message = window.prompt("检查点说明", "Agent checkpoint");
  if (!message) return;
  await apiPost(`/api/conversations/${currentConversationId.value}/workspace/git/checkpoint`, { message });
  await refreshGitStatus();
  reviewVisible.value = false;
}

async function restoreGitFile(path) {
  if (!window.confirm(`撤销“${path}”的全部未提交修改？`)) return;
  await apiPost(`/api/conversations/${currentConversationId.value}/workspace/git/restore`, { path });
  reviewVisible.value = false;
  await refreshWorkspace();
  if (workspaceFilePath.value === path) await openWorkspaceFile(path);
}

async function restoreTrashItem(trashId) {
  await apiPost(`/api/conversations/${currentConversationId.value}/workspace/trash/restore`, { trash_id: trashId });
  await refreshWorkspace();
}

async function saveWorkspaceFile() {
  if (!currentConversationId.value || !workspaceFilePath.value || workspaceSaving.value) return;
  workspaceSaving.value = true;
  try {
    await apiPost(`/api/conversations/${currentConversationId.value}/workspace/file`, { path: workspaceFilePath.value, content: workspaceContent.value });
    workspaceOriginal.value = workspaceContent.value;
    await refreshWorkspace();
  } finally {
    workspaceSaving.value = false;
  }
}

async function createWorkspaceFile() {
  const name = newWorkspacePath.value.trim().replaceAll("\\", "/");
  const path = workspaceCreateBase.value ? `${workspaceCreateBase.value}/${name}` : name;
  if (!name || !currentConversationId.value) return;
  await apiPost(`/api/conversations/${currentConversationId.value}/workspace/file`, { path, content: "" });
  newWorkspacePath.value = "";
  workspaceCreateMode.value = "";
  workspaceCreateBase.value = "";
  await refreshWorkspace();
  await openWorkspaceFile(path);
}

async function createWorkspaceDirectory() {
  const name = newWorkspacePath.value.trim().replaceAll("\\", "/");
  const path = workspaceCreateBase.value ? `${workspaceCreateBase.value}/${name}` : name;
  if (!name || !currentConversationId.value) return;
  await apiPost(`/api/conversations/${currentConversationId.value}/workspace/directory`, { path });
  newWorkspacePath.value = "";
  workspaceCreateMode.value = "";
  workspaceCreateBase.value = "";
  await refreshWorkspace();
}

async function deleteWorkspaceEntry(entry) {
  if (!currentConversationId.value) return;
  const label = entry.type === "directory" ? "文件夹及其中全部内容" : "文件";
  if (!window.confirm(`确定删除${label}“${entry.path}”吗？`)) return;
  await apiDelete(`/api/conversations/${currentConversationId.value}/workspace/path?path=${encodeURIComponent(entry.path)}`);
  if (workspaceFilePath.value === entry.path || workspaceFilePath.value.startsWith(`${entry.path}/`)) {
    workspaceFilePath.value = "";
    workspaceContent.value = "";
    workspaceOriginal.value = "";
    workspaceOutput.value = "";
  }
  if (selectedWorkspaceDirectory.value === entry.path || selectedWorkspaceDirectory.value.startsWith(`${entry.path}/`)) {
    selectedWorkspaceDirectory.value = "";
  }
  await refreshWorkspace();
}

async function moveWorkspaceEntry(targetDirectory = "") {
  const entry = draggingWorkspaceEntry.value;
  draggingWorkspaceEntry.value = null;
  if (!entry || !currentConversationId.value) return;
  const name = entry.path.split("/").pop();
  const target = targetDirectory ? `${targetDirectory}/${name}` : name;
  if (target === entry.path || targetDirectory === entry.path || targetDirectory.startsWith(`${entry.path}/`)) return;
  await apiPost(`/api/conversations/${currentConversationId.value}/workspace/move`, {
    source: entry.path,
    target,
  });
  if (workspaceFilePath.value === entry.path || workspaceFilePath.value.startsWith(`${entry.path}/`)) {
    workspaceFilePath.value = target + workspaceFilePath.value.slice(entry.path.length);
  }
  if (selectedWorkspaceDirectory.value === entry.path || selectedWorkspaceDirectory.value.startsWith(`${entry.path}/`)) {
    selectedWorkspaceDirectory.value = target + selectedWorkspaceDirectory.value.slice(entry.path.length);
  }
  await refreshWorkspace();
}

async function dropWorkspaceAtRoot(event) {
  if (event.target.closest?.(".tree-row")) return;
  await moveWorkspaceEntry("");
}

async function runWorkspaceFile() {
  if (!canRunWorkspaceFile.value || workspaceRunning.value) return;
  workspaceRunning.value = true;
  workspaceOutput.value = "正在运行…";
  try {
    if (workspaceDirty.value) await saveWorkspaceFile();
    const result = await apiPost(`/api/conversations/${currentConversationId.value}/workspace/run`, { path: workspaceFilePath.value });
    const sections = [];
    if (result.stdout) sections.push(result.stdout.trimEnd());
    if (result.stderr) sections.push(`[stderr]\n${result.stderr.trimEnd()}`);
    sections.push(`[exit code: ${result.exit_code}]`);
    workspaceOutput.value = sections.join("\n");
  } catch (error) {
    workspaceOutput.value = `运行失败：${error.message || error}`;
  } finally {
    workspaceRunning.value = false;
  }
}

async function loadTerminals() {
  if (mode.value !== "coding-agent" || !currentConversationId.value) return;
  const data = await apiGet(`/api/conversations/${currentConversationId.value}/workspace/terminals`);
  terminals.value = data.terminals || [];
  if (!terminals.value.length) {
    await createTerminal();
    return;
  }
  if (!terminals.value.some((terminal) => terminal.id === activeTerminalId.value)) {
    activeTerminalId.value = terminals.value[0].id;
  }
  await scrollTerminal();
}

async function createTerminal() {
  if (!currentConversationId.value || mode.value !== "coding-agent" || terminalCreating.value) return;
  const conversationId = currentConversationId.value;
  terminalCreating.value = true;
  try {
    const terminal = await apiPost(`/api/conversations/${conversationId}/workspace/terminals`, {});
    if (currentConversationId.value !== conversationId || mode.value !== "coding-agent") return;
    terminals.value.push(terminal);
    activeTerminalId.value = terminal.id;
    await scrollTerminal();
  } finally {
    terminalCreating.value = false;
  }
}

async function closeTerminal(terminalId) {
  const terminal = terminals.value.find((item) => item.id === terminalId);
  if (!terminal || !window.confirm(`关闭 ${terminal.title}？该终端中的运行进程也会结束。`)) return;
  await apiDelete(`/api/conversations/${currentConversationId.value}/workspace/terminals/${terminalId}`);
  terminals.value = terminals.value.filter((item) => item.id !== terminalId);
  if (activeTerminalId.value === terminalId) activeTerminalId.value = terminals.value[0]?.id || "";
}

async function executeTerminalCommand() {
  const terminal = activeTerminal.value;
  const command = terminalCommand.value.trim();
  if (!terminal || !command || terminalRunning.value) return;
  terminalCommand.value = "";
  if (["clear", "cls"].includes(command.toLowerCase())) {
    terminal.output = "";
    await scrollTerminal();
    return;
  }
  terminalRunning.value = true;
  try {
    await streamTerminal(
      `/api/conversations/${currentConversationId.value}/workspace/terminals/${terminal.id}/execute/stream`,
      { command },
      async (event) => {
        if (event.event === "output") terminal.output += event.content || "";
        if (event.event === "terminal" && event.content) {
          const index = terminals.value.findIndex((item) => item.id === terminal.id);
          if (index >= 0) terminals.value[index] = event.content;
        }
        if (event.event === "error") terminal.output += `\n[terminal error] ${event.content}\n`;
        await scrollTerminal();
      },
    );
    await refreshWorkspace();
  } catch (error) {
    terminal.output += `\n[terminal error] ${error.message || error}\n`;
  } finally {
    terminalRunning.value = false;
    await scrollTerminal();
  }
}

async function interruptTerminal() {
  const terminal = activeTerminal.value;
  if (!terminal || !terminalRunning.value) return;
  await apiPost(`/api/conversations/${currentConversationId.value}/workspace/terminals/${terminal.id}/interrupt`, {});
  terminal.output += "\n[terminal] interrupted by user\n";
}

async function scrollTerminal() {
  await nextTick();
  if (terminalEl.value) terminalEl.value.scrollTop = terminalEl.value.scrollHeight;
}

function buildWorkspaceTree(entries) {
  const roots = [];
  const nodes = new Map();
  const ordered = [...entries].sort((left, right) => left.path.localeCompare(right.path));
  for (const entry of ordered) {
    const parts = entry.path.split("/").filter(Boolean);
    let children = roots;
    let currentPath = "";
    parts.forEach((name, index) => {
      currentPath = currentPath ? `${currentPath}/${name}` : name;
      let node = nodes.get(currentPath);
      if (!node) {
        node = {
          name,
          path: currentPath,
          type: index === parts.length - 1 ? entry.type : "directory",
          children: [],
        };
        nodes.set(currentPath, node);
        children.push(node);
      } else if (index === parts.length - 1) {
        node.type = entry.type;
      }
      children = node.children;
    });
  }
  const sortNodes = (items) => {
    items.sort((left, right) => {
      if (left.type !== right.type) return left.type === "directory" ? -1 : 1;
      return left.name.localeCompare(right.name);
    });
    items.forEach((item) => sortNodes(item.children));
  };
  sortNodes(roots);
  return roots;
}

function startResize(kind, event) {
  if (window.innerWidth <= 980) return;
  event.preventDefault();
  resizeCleanup?.();
  const startX = event.clientX;
  const startY = event.clientY;
  const initial = {
    sidebar: sidebarWidth.value,
    coding: codingPaneWidth.value,
    files: fileTreeWidth.value,
    terminal: terminalHeight.value,
  }[kind];
  const containerWidth = event.currentTarget.parentElement?.clientWidth || window.innerWidth;
  document.body.classList.add("is-resizing", `resize-${kind}`);

  const move = (moveEvent) => {
    if (kind === "sidebar") sidebarWidth.value = clamp(initial + moveEvent.clientX - startX, 220, 420);
    if (kind === "coding") {
      codingPaneWidth.value = clamp(initial + moveEvent.clientX - startX, 340, Math.max(340, containerWidth - 460));
    }
    if (kind === "files") {
      fileTreeWidth.value = clamp(initial + moveEvent.clientX - startX, 160, Math.max(160, containerWidth - 320));
    }
    if (kind === "terminal") {
      const maxHeight = Math.max(180, (transcriptPanelEl.value?.clientHeight || 700) * 0.58);
      terminalHeight.value = clamp(initial - (moveEvent.clientY - startY), 150, maxHeight);
    }
  };
  const stop = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", stop);
    window.removeEventListener("pointercancel", stop);
    document.body.classList.remove("is-resizing", `resize-${kind}`);
    resizeCleanup = null;
  };
  resizeCleanup = stop;
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", stop);
  window.addEventListener("pointercancel", stop);
}

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

async function scrollMessages() {
  await nextTick();
  if (messagesEl.value) messagesEl.value.scrollTop = messagesEl.value.scrollHeight;
}
</script>
