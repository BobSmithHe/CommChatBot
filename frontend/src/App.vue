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
          <Activity v-if="isConversationRunning(item.id)" class="conversation-running" :size="13" aria-label="任务运行中" />
          <GitFork v-if="!showArchived" :size="13" title="从当前节点创建会话分支" @click.stop="forkConversation(item)" />
          <ArchiveRestore v-if="showArchived" :size="14" @click.stop="restoreConversation(item.id)" />
          <Trash2 v-else :size="14" @click.stop="deleteConversation(item.id)" />
        </button>
      </div>
      <div class="account-card">
        <div>
          <UserRound :size="15" />
          <span>{{ currentUser?.username || "未登录" }}</span>
        </div>
        <div class="account-actions">
          <button type="button" title="记忆管理" @click="openMemoryPanel"><Brain :size="14" /></button>
          <button v-if="currentUser?.username !== 'anonymous'" type="button" title="退出登录" @click="logout">
            <LogOut :size="14" />
          </button>
          <button v-else type="button" title="登录或注册" @click="openAuth('login')">
            <LogIn :size="14" />
          </button>
        </div>
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
        <button
          v-if="mode === 'coding-agent' && projectContextStatus.discovered?.length"
          :class="['project-trust', { trusted: projectContextStatus.project_trusted }]"
          type="button"
          @click="toggleProjectTrust"
        >{{ projectContextStatus.project_trusted ? "项目已信任" : "信任项目上下文" }}</button>
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
                    <span>运行轨迹 · {{ message.events?.length ? `${traceTurns(message.events).length} 轮` : "无历史记录" }}</span>
                    <ChevronDown v-if="message.traceExpanded" :size="14" />
                    <ChevronRight v-else :size="14" />
                  </button>
                  <div v-show="message.traceExpanded" class="trace-turns">
                    <AgentTurn v-for="turn in traceTurns(message.events)" :key="turn.key" :turn="turn" />
                    <div v-if="!message.events?.length" class="trace-empty">{{ isActiveAssistant(message) ? "正在等待第一轮…" : "该回答生成于轨迹记录启用前。" }}</div>
                  </div>
                </div>
                <div class="markdown" v-html="renderMessageMarkdown(message)" @click="handleMarkdownAction" />
              </div>
            </article>
          </div>

          <form class="composer" @submit.prevent="sendMessage">
            <div v-if="activeApproval" class="approval-dock" role="alertdialog" aria-live="assertive" aria-label="工具执行审批">
              <div class="approval-dock-icon"><ShieldCheck :size="17" /></div>
              <div class="approval-dock-body">
                <strong>{{ approvalTitle(activeApproval) }}</strong>
                <code>{{ approvalSummary(activeApproval) }}</code>
              </div>
              <div class="approval-dock-actions">
                <button type="button" class="reject" @click="resolveApproval(activeApproval, false)">拒绝</button>
                <button type="button" class="approve" @click="resolveApproval(activeApproval, true)">允许</button>
              </div>
            </div>
            <div v-if="attachedFiles.length" class="attachment-list">
              <div v-for="file in attachedFiles" :key="file.id" class="attachment-chip">
                <FileText :size="14" />
                <span>{{ file.name }}</span>
                <button type="button" aria-label="移除附件" @click="removeAttachment(file.id)"><X :size="13" /></button>
              </div>
            </div>
            <textarea
              v-model="draft"
              :disabled="attachmentUploading"
              rows="3"
              :placeholder="isStreaming ? '追加指令到正在运行的任务…' : '输入无线通信问题、仿真需求或代码任务...'"
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
              <div v-if="isStreaming" class="running-composer-actions">
                <select v-model="queuedMessageKind" title="追加消息类型">
                  <option value="steering">调整当前任务</option>
                  <option value="follow_up">完成后继续</option>
                </select>
                <button class="send-button" type="submit" :disabled="!draft.trim() || !currentTaskId">
                  <SendHorizontal :size="15" /><span>追加</span>
                </button>
                <button class="send-button stop" type="button" @click="cancelCurrentTask">
                  <Square :size="14" /><span>停止</span>
                </button>
              </div>
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
              <span :class="['terminal-connection', terminalStatus]">{{ terminalStatus === 'connected' ? 'ConPTY' : terminalStatus }}</span>
              <span
                v-if="activeTerminal?.restarted"
                class="terminal-restarted"
                :title="`Shell 已重启；环境状态可能丢失（${activeTerminal.restart_reason || 'unknown'}）`"
              >shell restarted</span>
              <button v-if="activeTerminal" class="terminal-new danger" type="button" title="发送 Ctrl+C（终端保持可用）" @click="interruptTerminal"><Square :size="12" /></button>
            </div>
            <TerminalView
              v-if="activeTerminal"
              ref="terminalView"
              :conversation-id="currentConversationId"
              :terminal-id="activeTerminal.id"
              @ready="updateActiveTerminal"
              @status="terminalStatus = $event"
            />
            <div v-else class="terminal-empty">点击 + 新建终端</div>
          </section>
        </div>

        <div v-if="mode === 'coding-agent'" class="pane-resizer coding-resizer" title="拖动调整对话与编辑器宽度" @pointerdown="startResize('coding', $event)" />

        <aside v-if="mode === 'coding-agent'" class="workspace-editor">
          <div class="editor-header">
            <div><strong>EXPLORER</strong><span>{{ workspaceId ? workspaceId.slice(0, 8) : "创建中" }}</span></div>
          </div>
          <div class="editor-layout" :style="{ gridTemplateColumns: `${fileTreeWidth}px 1px minmax(0, 1fr)` }">
            <div class="file-explorer" @dragover.prevent @drop="dropWorkspaceAtRoot">
              <div class="explorer-toolbar" aria-label="Explorer 工具栏">
              <button type="button" title="导入目录、clone 或 worktree" @click="importWorkspace"><FolderOpen :size="15" /></button>
              <button type="button" title="切换或新建分支" @click="switchWorkspaceBranch"><GitBranch :size="15" /></button>
              <button type="button" title="新建文件" @click="beginWorkspaceCreate('file')"><FilePlus2 :size="15" /></button>
              <button type="button" title="新建文件夹" @click="beginWorkspaceCreate('directory')"><FolderPlus :size="15" /></button>
              <button type="button" title="刷新" :disabled="workspaceLoading" @click="refreshWorkspace"><RefreshCw :size="15" /></button>
              <button type="button" title="查看更改" @click="openWorkspaceChanges"><GitCompare :size="15" /></button>
              <button type="button" title="创建 Git 检查点" @click="checkpointWorkspace"><ShieldCheck :size="15" /></button>
              <button type="button" title="折叠全部" @click="collapseWorkspaceTree"><ChevronsUp :size="15" /></button>
              </div>
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
              <div v-if="!workspaceTree.length && !workspaceLoading" class="workspace-empty">空工作区。使用上方工具栏创建文件或文件夹。</div>
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
                <div class="editor-tabs">
                  <button v-for="tab in openFiles" :key="tab.path" :class="['editor-tab', { active: tab.path === workspaceFilePath }]" type="button" :title="tab.path" @click="activateWorkspaceTab(tab.path)">
                    <span>{{ tab.path.split('/').pop() }}</span><i v-if="tab.conflict" class="tab-conflict" title="文件已被外部修改">!</i><i v-else-if="tab.content !== tab.original">●</i>
                    <X :size="12" @click.stop="closeWorkspaceTab(tab.path)" />
                  </button>
                  <span v-if="!openFiles.length" class="no-editor-tabs">未打开文件</span>
                </div>
                <div class="file-actions">
                  <button type="button" title="格式化文档 (Shift+Alt+F)" :disabled="!workspaceFilePath" @click="formatWorkspaceFile"><Sparkles :size="14" /></button>
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
              <MonacoEditor v-else-if="workspaceFilePath" ref="monacoEditor" v-model="workspaceContent" :path="workspaceFilePath" :conversation-id="currentConversationId" @save="saveWorkspaceFile" @navigate="handleEditorNavigate" />
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

  <div v-if="memoryOpen" class="auth-overlay memory-overlay" @click.self="memoryOpen = false">
    <section class="memory-dialog" aria-label="记忆管理">
      <header class="auth-heading">
        <div><span>Memory</span><h2>记忆管理</h2></div>
        <button type="button" title="关闭" @click="memoryOpen = false"><X :size="16" /></button>
      </header>
      <div class="memory-privacy">
        <label><input v-model="memorySettings.enabled" type="checkbox" @change="saveMemorySettings" />启用记忆召回</label>
        <label><input v-model="memorySettings.auto_capture" type="checkbox" :disabled="!memorySettings.enabled" @change="saveMemorySettings" />自动提取长期记忆</label>
        <label><input v-model="memorySettings.semantic_recall" type="checkbox" :disabled="!memorySettings.enabled" @change="saveMemorySettings" />Milvus 语义召回</label>
      </div>
      <details class="memory-scopes">
        <summary>作用域与隐私</summary>
        <div>
          <label><input v-model="memorySettings.user_scope" type="checkbox" @change="saveMemorySettings" />用户</label>
          <label><input v-model="memorySettings.project_scope" type="checkbox" @change="saveMemorySettings" />项目</label>
          <label><input v-model="memorySettings.conversation_scope" type="checkbox" @change="saveMemorySettings" />会话</label>
          <label><input v-model="memorySettings.task_scope" type="checkbox" @change="saveMemorySettings" />任务</label>
        </div>
        <p>记忆仅对当前账户可见；疑似密钥、密码和私钥不会保存。关闭作用域后停止召回和自动写入，但不会自动删除历史数据。</p>
      </details>
      <div class="memory-toolbar">
        <select v-model="memoryFilter" @change="refreshMemories">
          <option value="">全部层级</option><option value="user">用户</option><option value="project">项目</option>
          <option value="conversation">会话</option><option value="task">任务</option>
        </select>
        <input v-model.trim="memoryQuery" placeholder="搜索记忆" @keydown.enter.prevent="refreshMemories" />
        <button type="button" @click="refreshMemories"><RefreshCw :size="14" /></button>
        <button type="button" @click="beginMemoryCreate"><Plus :size="14" />新增</button>
        <button type="button" class="danger" @click="clearMemoryScope">清空</button>
      </div>
      <form v-if="memoryEditing" class="memory-form" @submit.prevent="saveMemory">
        <div>
          <select v-model="memoryForm.scope" :disabled="memoryEditing !== 'new'">
            <option value="user">用户</option>
            <option value="project" :disabled="mode !== 'coding-agent' || !currentConversationId">项目</option>
            <option value="conversation" :disabled="!currentConversationId">会话</option>
            <option value="task" :disabled="!currentTaskId">任务</option>
          </select>
          <select v-model="memoryForm.category">
            <option value="preference">偏好</option><option value="fact">事实</option><option value="decision">决策</option>
            <option value="workflow">工作流</option><option value="constraint">约束</option>
          </select>
        </div>
        <input v-model.trim="memoryForm.key" maxlength="200" placeholder="记忆名称" required />
        <textarea v-model.trim="memoryForm.value" maxlength="8000" rows="3" placeholder="记忆内容" required />
        <div class="memory-form-meta">
          <label>重要度 <input v-model.number="memoryForm.importance" type="range" min="0" max="1" step="0.1" /></label>
          <label>有效天数 <input v-model.number="memoryForm.ttl_days" type="number" min="0" max="3650" /></label>
          <button type="button" @click="memoryEditing = ''">取消</button>
          <button type="submit">保存</button>
        </div>
      </form>
      <p v-if="memoryError" class="auth-error">{{ memoryError }}</p>
      <div class="memory-list">
        <article v-for="item in memoryItems" :key="item.id" class="memory-item">
          <div class="memory-item-heading">
            <div><span>{{ memoryScopeLabel(item.scope) }} · {{ memoryCategoryLabel(item.category) }}</span><strong>{{ item.key }}</strong></div>
            <div><button type="button" title="编辑" @click="editMemory(item)"><Pencil :size="13" /></button><button type="button" title="删除" @click="removeMemory(item)"><Trash2 :size="13" /></button></div>
          </div>
          <p>{{ item.value }}</p>
          <small>置信度 {{ Math.round(item.confidence * 100) }}% · 重要度 {{ Math.round(item.importance * 100) }}% · v{{ item.revision }} · 使用 {{ item.access_count }} 次</small>
        </article>
        <div v-if="!memoryLoading && !memoryItems.length" class="trace-empty">暂无匹配记忆</div>
        <div v-if="memoryLoading" class="trace-empty">正在加载…</div>
      </div>
    </section>
  </div>

  <div v-if="authOpen" class="auth-overlay" @click.self="closeAuth">
    <form class="auth-dialog" @submit.prevent="submitAuth">
      <div class="auth-heading">
        <div>
          <span>CommChatBot</span>
          <h2>{{ authTitle }}</h2>
        </div>
        <button v-if="currentUser" type="button" title="关闭" @click="closeAuth"><X :size="16" /></button>
      </div>
      <label v-if="authMode === 'login' || authMode === 'register'">
        <span>用户名</span>
        <input v-model.trim="authForm.username" name="username" autocomplete="username" minlength="2" required />
      </label>
      <label v-if="authMode === 'register'">
        <span>邮箱</span>
        <input v-model.trim="authForm.email" name="email" type="email" autocomplete="email" required />
      </label>
      <label v-if="authMode === 'forgot'">
        <span>用户名或邮箱</span>
        <input v-model.trim="authForm.identity" name="identity" autocomplete="username" required />
      </label>
      <label v-if="authMode === 'reset'">
        <span>重置令牌</span>
        <input v-model.trim="authForm.resetToken" name="reset-token" autocomplete="off" required />
      </label>
      <label v-if="authMode !== 'forgot'">
        <span>密码</span>
        <input v-model="authForm.password" name="password" type="password" :autocomplete="authMode === 'login' ? 'current-password' : 'new-password'" minlength="8" required />
      </label>
      <p v-if="authError" class="auth-error">{{ authError }}</p>
      <p v-if="authNotice" class="auth-notice">{{ authNotice }}</p>
      <button class="auth-submit" type="submit" :disabled="authSubmitting">
        {{ authSubmitting ? '处理中…' : authSubmitLabel }}
      </button>
      <button v-if="authMode === 'login'" class="auth-switch" type="button" @click="openAuth('forgot')">忘记密码？</button>
      <button class="auth-switch" type="button" @click="switchAuthMode">
        {{ authMode === 'login' ? '没有账户？创建一个' : '已有账户？返回登录' }}
      </button>
    </form>
  </div>
</template>

<script setup>
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import {
  Activity, ArchiveRestore, Bot, Brain, ChevronDown, ChevronRight, ChevronsUp, Database, FilePlus2, FileText,
  FolderOpen, FolderPlus, GitBranch, GitCompare, GitFork, Globe2, LogIn, LogOut, Paperclip, Play, Plus, RefreshCw, RotateCcw, Save,
  Pencil, SendHorizontal, ShieldCheck, Sparkles, Square, Trash2, UserRound, X,
} from "lucide-vue-next";
import {
  apiDelete, apiGet, apiPatch, apiPost, clearAuthSession, getRefreshToken, setAuthSession,
  streamChat, uploadFile,
} from "./api/client";
import WorkspaceTreeNode from "./components/WorkspaceTreeNode.vue";
import TerminalView from "./components/TerminalView.vue";
import AgentTurn from "./components/AgentTurn.vue";
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
const recoverableTask = ref(null);
const pendingResumeTaskId = ref("");
const activeApproval = ref(null);
const queuedMessageKind = ref("steering");
const projectContextStatus = ref({ project_trusted: false, discovered: [] });
const currentUser = ref(null);
const authOpen = ref(false);
const authMode = ref("login");
const authSubmitting = ref(false);
const authError = ref("");
const authNotice = ref("");
const authForm = ref({ username: "", email: "", password: "", identity: "", resetToken: "" });
const memoryOpen = ref(false);
const memoryLoading = ref(false);
const memoryError = ref("");
const memoryItems = ref([]);
const memoryFilter = ref("");
const memoryQuery = ref("");
const memoryEditing = ref("");
const memorySettings = ref({ enabled: true, auto_capture: true, semantic_recall: true, user_scope: true, project_scope: true, conversation_scope: true, task_scope: true });
const memoryForm = ref({ scope: "user", category: "fact", key: "", value: "", importance: 0.5, ttl_days: 0 });
const messages = ref([]);
const messagesEl = ref(null);
const terminalView = ref(null);
const monacoEditor = ref(null);
const codingLayoutEl = ref(null);
const transcriptPanelEl = ref(null);
const workspaceCreateInputEl = ref(null);
const draft = ref("");
// A task belongs to its conversation, not to whichever transcript happens to
// be visible. Keeping these records separate lets users switch conversations
// without detaching or cancelling the original background task.
const conversationRuns = reactive(new Map());
const conversationMessageCache = new Map();
const attachedFiles = ref([]);
const attachmentUploading = ref(false);
const attachmentError = ref("");
const workspaceId = ref("");
const workspaceEntries = ref([]);
const workspaceFilePath = ref("");
const openFiles = ref([]);
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
const terminalStatus = ref("disconnected");
const terminalCreating = ref(false);
const sidebarWidth = ref(280);
const codingPaneWidth = ref(500);
const fileTreeWidth = ref(220);
const terminalHeight = ref(248);
let resizeCleanup = null;
let workspaceWatchTimer = null;
let localId = 0;
let eventId = 0;
const lastConversationIds = { chatbot: null, "coding-agent": null };

const activeRun = computed(() => conversationRuns.get(String(currentConversationId.value)) || null);
const isStreaming = computed(() => Boolean(activeRun.value));
const currentTaskId = computed(() => activeRun.value?.taskId || "");
const workspaceDirty = computed(() => workspaceContent.value !== workspaceOriginal.value);
const authTitle = computed(() => ({ login: "登录", register: "创建账户", forgot: "找回密码", reset: "设置新密码" }[authMode.value]));
const authSubmitLabel = computed(() => ({ login: "登录", register: "注册并登录", forgot: "获取重置令牌", reset: "重置密码" }[authMode.value]));
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
  workspaceWatchTimer = window.setInterval(pollWorkspaceChanges, 2000);
});

onBeforeUnmount(() => {
  resizeCleanup?.();
  if (workspaceWatchTimer) window.clearInterval(workspaceWatchTimer);
});

watch(mode, async () => {
  cacheCurrentConversation();
  showArchived.value = false;
  currentConversationId.value = null;
  messages.value = [];
  attachedFiles.value = [];
  attachmentError.value = "";
  activeApproval.value = null;
  clearWorkspaceState();
  await loadConversations(true);
});

watch(workspaceContent, (content) => {
  const tab = openFiles.value.find((item) => item.path === workspaceFilePath.value);
  if (tab) tab.content = content;
});

watch(workspaceOriginal, (original) => {
  const tab = openFiles.value.find((item) => item.path === workspaceFilePath.value);
  if (tab) tab.original = original;
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
    clearAuthSession();
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
  authNotice.value = "";
  authForm.value = { username: "", email: "", password: "", identity: "", resetToken: "" };
  authOpen.value = true;
}

function closeAuth() {
  if (currentUser.value) authOpen.value = false;
}

function switchAuthMode() {
  authMode.value = authMode.value === "login" ? "register" : "login";
  authError.value = "";
  authNotice.value = "";
}

async function submitAuth() {
  if (authSubmitting.value) return;
  authSubmitting.value = true;
  authError.value = "";
  authNotice.value = "";
  try {
    if (authMode.value === "forgot") {
      const result = await apiPost("/api/auth/password/forgot", { identity: authForm.value.identity });
      authNotice.value = result.message;
      if (result.reset_token) {
        authForm.value.resetToken = result.reset_token;
        authMode.value = "reset";
        authNotice.value = "本地开发重置令牌已自动填入。";
      }
      return;
    }
    if (authMode.value === "reset") {
      await apiPost("/api/auth/password/reset", {
        reset_token: authForm.value.resetToken,
        new_password: authForm.value.password,
      });
      authMode.value = "login";
      authNotice.value = "密码已重置，请重新登录。";
      authForm.value.password = "";
      return;
    }
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
    setAuthSession(session);
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
  if (conversationRuns.size) return;
  const refreshToken = getRefreshToken();
  if (refreshToken) {
    try {
      await apiPost("/api/auth/logout", { refresh_token: refreshToken });
    } catch {
      // Local session must still be cleared if the server is unavailable.
    }
  }
  clearAuthSession();
  if (await loadCurrentUser()) await reloadIdentityScope();
}

async function reloadIdentityScope() {
  conversationMessageCache.clear();
  conversations.value = [];
  currentConversationId.value = null;
  messages.value = [];
  attachedFiles.value = [];
  activeApproval.value = null;
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
  conversationMessageCache.set(String(conv.id), []);
  permissionMode.value = conv.permission_mode || "workspace-write";
  lastConversationIds[requestedMode] = conv.id;
  messages.value = [];
  attachedFiles.value = [];
  attachmentError.value = "";
  activeApproval.value = null;
  clearWorkspaceState();
  if (requestedMode === "coding-agent") {
    await refreshWorkspace();
    await loadTerminals();
    await loadProjectContextStatus();
  }
}

async function switchConversation(id) {
  cacheCurrentConversation();
  const data = await apiGet(`/api/conversations/${id}`);
  if (data.mode !== mode.value) return;
  currentConversationId.value = id;
  permissionMode.value = data.permission_mode || "workspace-write";
  lastConversationIds[mode.value] = id;
  const cachedMessages = conversationMessageCache.get(String(id));
  messages.value = cachedMessages || hydrateMessages(data.messages);
  conversationMessageCache.set(String(id), messages.value);
  attachedFiles.value = [];
  attachmentError.value = "";
  activeApproval.value = null;
  clearWorkspaceState();
  if (mode.value === "coding-agent") {
    await refreshWorkspace();
    await loadTerminals();
    await loadProjectContextStatus();
  }
  await loadRecoverableTask();
  const run = conversationRuns.get(String(id));
  if (run?.activeApproval) activeApproval.value = run.activeApproval;
  await scrollMessages();
}

async function deleteConversation(id) {
  await apiDelete(`/api/conversations/${id}`);
  conversations.value = conversations.value.filter((item) => item.id !== id);
  conversationMessageCache.delete(String(id));
  if (currentConversationId.value === id) {
    if (conversations.value.length) await switchConversation(conversations.value[0].id);
    else await newConversation();
  }
}

async function toggleArchived() {
  cacheCurrentConversation();
  showArchived.value = !showArchived.value;
  currentConversationId.value = null;
  messages.value = [];
  activeApproval.value = null;
  await loadConversations(true);
}

async function openMemoryPanel() {
  memoryOpen.value = true;
  memoryError.value = "";
  try {
    const [settings] = await Promise.all([apiGet("/api/memories/settings"), refreshMemories()]);
    memorySettings.value = settings;
  } catch (error) {
    memoryError.value = readableError(error);
  }
}

async function refreshMemories() {
  memoryLoading.value = true;
  memoryError.value = "";
  try {
    const params = new URLSearchParams();
    if (memoryFilter.value) params.set("scope", memoryFilter.value);
    if (memoryQuery.value) params.set("query", memoryQuery.value);
    if (memoryFilter.value === "conversation" && currentConversationId.value) {
      params.set("conversation_id", String(currentConversationId.value));
    }
    if (memoryFilter.value === "project" && mode.value === "coding-agent" && currentConversationId.value) {
      params.set("conversation_id", String(currentConversationId.value));
    }
    if (memoryFilter.value === "task" && currentTaskId.value) params.set("task_id", currentTaskId.value);
    const result = await apiGet(`/api/memories${params.size ? `?${params}` : ""}`);
    memoryItems.value = result.memories || [];
  } catch (error) {
    memoryError.value = readableError(error);
  } finally {
    memoryLoading.value = false;
  }
}

async function saveMemorySettings() {
  try {
    memorySettings.value = await apiPatch("/api/memories/settings", memorySettings.value);
  } catch (error) {
    memoryError.value = readableError(error);
  }
}

function beginMemoryCreate() {
  memoryEditing.value = "new";
  memoryForm.value = { scope: "user", category: "fact", key: "", value: "", importance: 0.5, ttl_days: 0 };
}

function editMemory(item) {
  memoryEditing.value = item.id;
  memoryForm.value = {
    scope: item.scope,
    category: item.category,
    key: item.key,
    value: item.value,
    importance: item.importance,
    ttl_days: 0,
  };
}

async function saveMemory() {
  memoryError.value = "";
  const payload = { ...memoryForm.value };
  try {
    if (memoryEditing.value === "new") {
      if (["project", "conversation"].includes(payload.scope)) {
        payload.conversation_id = currentConversationId.value;
      }
      if (payload.scope === "task") payload.task_id = currentTaskId.value;
      await apiPost("/api/memories", payload);
    } else {
      delete payload.scope;
      await apiPatch(`/api/memories/${memoryEditing.value}`, payload);
    }
    memoryEditing.value = "";
    await refreshMemories();
  } catch (error) {
    memoryError.value = readableError(error);
  }
}

async function removeMemory(item) {
  if (!window.confirm(`删除记忆“${item.key}”？`)) return;
  try {
    await apiDelete(`/api/memories/${item.id}`);
    await refreshMemories();
  } catch (error) {
    memoryError.value = readableError(error);
  }
}

async function clearMemoryScope() {
  const label = memoryFilter.value ? memoryScopeLabel(memoryFilter.value) : "全部";
  if (!window.confirm(`清空${label}记忆？该操作会停止这些记忆的召回。`)) return;
  try {
    const suffix = memoryFilter.value ? `?scope=${encodeURIComponent(memoryFilter.value)}` : "";
    await apiDelete(`/api/memories${suffix}`);
    await refreshMemories();
  } catch (error) {
    memoryError.value = readableError(error);
  }
}

function memoryScopeLabel(scope) {
  return ({ user: "用户", project: "项目", conversation: "会话", task: "任务" })[scope] || scope;
}

function memoryCategoryLabel(category) {
  return ({ preference: "偏好", fact: "事实", decision: "决策", workflow: "工作流", constraint: "约束" })[category] || category;
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
  if (!content) return;
  if (isStreaming.value) {
    if (!currentTaskId.value) return;
    await apiPost(`/api/tasks/${currentTaskId.value}/messages`, {
      kind: queuedMessageKind.value,
      content,
    });
    draft.value = "";
    messages.value.push({
      localId: ++localId,
      role: "user",
      content,
      queuedKind: queuedMessageKind.value,
    });
    await scrollMessages();
    return;
  }
  if (!currentConversationId.value) await newConversation();
  const conversationId = currentConversationId.value;
  const requestMode = mode.value;

  draft.value = "";
  attachedFiles.value = [];
  attachmentError.value = "";
  const attachmentLine = attachmentSnapshot.length ? `\n\n📎 ${attachmentSnapshot.map((item) => item.name).join(", ")}` : "";
  if (!isResume) messages.value.push({ localId: ++localId, role: "user", content: content + attachmentLine });
  messages.value.push({ localId: ++localId, role: "assistant", content: "", events: [], traceExpanded: true });
  const assistant = messages.value[messages.value.length - 1];
  const run = reactive({ conversationId, mode: requestMode, taskId: "", assistant, activeApproval: null });
  conversationRuns.set(String(conversationId), run);
  conversationMessageCache.set(String(conversationId), messages.value);
  activeApproval.value = null;
  await scrollMessages();

  try {
    await streamChat(
      {
        message: content,
        conversation_id: conversationId,
        mode: requestMode,
        use_rag: requestMode === "chatbot" && useRag.value,
        use_web: requestMode === "chatbot" && useWeb.value,
        attachment_ids: !isResume && requestMode === "chatbot" ? attachmentSnapshot.map((item) => item.id) : [],
        resume_task_id: resumeTaskId || null,
      },
      async (event) => {
        if (event.event === "intermediate_answer") {
          run.assistant.content += event.content?.content || "";
          return;
        }
        if (event.event === "queued_message") {
          run.assistant.traceExpanded = false;
          const nextAssistant = { localId: ++localId, role: "assistant", content: "", events: [], traceExpanded: true };
          messages.value.push(nextAssistant);
          run.assistant = nextAssistant;
        }
        const activeAssistant = run.assistant;
        if (event.event === "task") run.taskId = event.content?.task_id || "";
        else if (event.event === "answer") activeAssistant.content += event.content || "";
        else if (event.event === "cancelled") activeAssistant.content += "\n\n_任务已停止。_";
        else if (
          [
            "turn_start", "turn_end", "thinking", "tool_use", "tool_update", "tool_result", "llm_response",
            "queued_message", "context_compacted", "message_queued", "memory_updated", "memory_recalled",
            "status", "result", "sources", "approval_required", "hook", "error",
          ].includes(event.event)
          && event.content
          && !String(event.content).startsWith("Connected.")
        ) {
          if (event.event === "turn_start") {
            activeAssistant.events.forEach((item) => {
              if (item.event === "turn_start") item.turnExpanded = false;
            });
          }
          if (event.event === "approval_required") {
            activeAssistant.events.forEach((item) => {
              if (item.approvalPending) {
                item.approvalPending = false;
                item.approvalDecision = "expired";
              }
            });
          }
          const traceEvent = {
            ...event,
            id: ++eventId,
            approvalPending: event.event === "approval_required",
            turnExpanded: event.event === "turn_start",
          };
          activeAssistant.events.push(traceEvent);
          if (event.event === "error" && !activeAssistant.content.trim()) {
            activeAssistant.content = `任务失败：${formatEventContent(event.content)}`;
          }
          if (event.event === "approval_required") {
            run.activeApproval = traceEvent;
            if (currentConversationId.value === conversationId) activeApproval.value = traceEvent;
          }
          if (event.event === "turn_end") {
            const turnIndex = Number(event.content?.index);
            const startEvent = [...activeAssistant.events].reverse().find((item) => (
              item.event === "turn_start" && Number(item.content?.index) === turnIndex
            ));
            if (startEvent) startEvent.turnExpanded = false;
          }
        }
        if (currentConversationId.value === conversationId) await scrollMessages();
      },
    );
    await loadConversations();
  } catch (error) {
    run.assistant.content = `请求失败：${error.message || error}`;
  } finally {
    run.assistant.traceExpanded = false;
    if (conversationRuns.get(String(conversationId)) === run) conversationRuns.delete(String(conversationId));
    if (currentConversationId.value === conversationId) {
      pendingResumeTaskId.value = "";
      activeApproval.value = null;
      if (requestMode === "coding-agent") await refreshWorkspace();
      await scrollMessages();
    }
  }
}

async function cancelCurrentTask() {
  if (!currentTaskId.value) return;
  await apiPost(`/api/tasks/${currentTaskId.value}/cancel`, {});
}

async function forkConversation(item) {
  const branchName = window.prompt("会话分支名称", "branch")?.trim();
  if (!branchName) return;
  const branch = await apiPost(`/api/conversations/${item.id}/fork`, { branch_name: branchName });
  if (branch.mode !== mode.value) return;
  conversations.value = [branch, ...conversations.value];
  await switchConversation(branch.id);
}

async function loadProjectContextStatus() {
  if (mode.value !== "coding-agent" || !currentConversationId.value) {
    projectContextStatus.value = { project_trusted: false, discovered: [] };
    return;
  }
  projectContextStatus.value = await apiGet(`/api/conversations/${currentConversationId.value}/project-context`);
}

async function toggleProjectTrust() {
  if (!currentConversationId.value) return;
  const trusted = !projectContextStatus.value.project_trusted;
  projectContextStatus.value = await apiPatch(`/api/conversations/${currentConversationId.value}/trust`, { trusted });
  await loadProjectContextStatus();
}

async function resolveApproval(event, approved) {
  const content = event.content || {};
  if (!content.task_id || !content.approval_id || !event.approvalPending) return;
  await apiPost(`/api/tasks/${content.task_id}/approvals/${content.approval_id}`, { approved });
  event.approvalPending = false;
  event.approvalDecision = approved ? "approved" : "rejected";
  if (activeApproval.value?.content?.approval_id === content.approval_id) activeApproval.value = null;
}

function traceTurns(events = []) {
  const turns = [];
  let current = null;
  let pending = [];
  for (const event of events || []) {
    if (event.event === "turn_start") {
      current = {
        key: `turn-${event.content?.index ?? turns.length}-${event.id}`,
        label: `Turn ${Number(event.content?.index ?? turns.length) + 1}`,
        anchor: event,
        events: pending,
        completed: false,
      };
      pending = [];
      turns.push(current);
      continue;
    }
    if (event.event === "turn_end") {
      const turnIndex = Number(event.content?.index);
      const target = [...turns].reverse().find((turn) => Number(turn.anchor.content?.index) === turnIndex);
      if (target) target.completed = true;
      if (target === current) current = null;
      continue;
    }
    if (current) current.events.push(event);
    else pending.push(event);
  }
  if (pending.length) {
    if (turns.length) turns[turns.length - 1].events.push(...pending);
    else turns.push({
      key: `legacy-${pending[0].id}`,
      label: "历史步骤",
      anchor: pending[0],
      events: pending,
      completed: !isStreaming.value,
    });
  }
  return turns;
}

function approvalTitle(event) {
  const tool = event?.content?.tool_name || "工具";
  return tool === "run_command" ? "允许运行命令？" : `允许调用 ${tool}？`;
}

function approvalSummary(event) {
  const input = event?.content?.input || {};
  if (Array.isArray(input.argv)) return input.argv.join(" ");
  if (typeof input.command === "string") return input.command;
  if (typeof input.code === "string") return input.code.split(/\r?\n/, 1)[0].slice(0, 180);
  return JSON.stringify(input);
}

function formatEventContent(content) {
  if (content && typeof content === "object" && ("text" in content || "tool_calls" in content)) {
    const parts = [];
    if (String(content.text || "").trim()) parts.push(String(content.text).trim());
    if (Array.isArray(content.tool_calls) && content.tool_calls.length) {
      parts.push(`请求调用工具：${content.tool_calls.map((call) => call.name).join(", ")}`);
    }
    if (content.recovered) parts.push("已从模型空响应中自动恢复");
    if (!parts.length) parts.push("模型未返回可展示内容");
    return parts.join("\n");
  }
  if (content && typeof content === "object") return JSON.stringify(content, null, 2);
  return String(content ?? "");
}

async function loadRecoverableTask() {
  recoverableTask.value = null;
  activeApproval.value = null;
  if (!currentConversationId.value) return;
  const tasks = await apiGet(`/api/tasks?conversation_id=${currentConversationId.value}`);
  const waitingTask = tasks.find((task) => task.status === "waiting-approval");
  if (waitingTask) {
    const details = await apiGet(`/api/tasks/${waitingTask.id}`);
    const approval = [...(details.approvals || [])].reverse().find((item) => item.status === "pending");
    if (approval) activeApproval.value = {
      id: ++eventId,
      event: "approval_required",
      approvalPending: true,
      content: {
        approval_id: approval.id,
        task_id: waitingTask.id,
        tool_name: approval.tool_name,
        input: approval.input,
        capability: approval.capability,
      },
    };
  }
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
  return activeRun.value?.assistant === message;
}

function isConversationRunning(conversationId) {
  return conversationRuns.has(String(conversationId));
}

function cacheCurrentConversation() {
  if (currentConversationId.value) {
    conversationMessageCache.set(String(currentConversationId.value), messages.value);
  }
}

function hydrateMessages(items = []) {
  return items
    .filter((item) => item.role === "user" || item.role === "assistant")
    .map((item) => ({
      ...item,
      localId: ++localId,
      events: (item.trace || []).map((event) => ({
        ...event,
        id: ++eventId,
        turnExpanded: event.event === "turn_start" ? false : undefined,
      })),
      traceExpanded: false,
    }));
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
  workspaceId.value = "";
  workspaceEntries.value = [];
  workspaceFilePath.value = "";
  openFiles.value = [];
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
  terminalStatus.value = "disconnected";
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
    await applyWorkspaceListing(data, true);
    await Promise.all([refreshGitStatus(), refreshTrash()]);
  } finally {
    workspaceLoading.value = false;
  }
}

async function importWorkspace() {
  if (!currentConversationId.value) return;
  const source = window.prompt("本地项目绝对路径，或 HTTPS Git URL");
  if (!source) return;
  const suggested = /^https?:\/\//i.test(source) ? "clone" : "local";
  const kind = window.prompt("导入方式：local / clone / worktree", suggested)?.trim().toLowerCase();
  if (!['local', 'clone', 'worktree'].includes(kind)) return;
  const branch = window.prompt("分支", "main")?.trim() || "main";
  await apiPost(`/api/conversations/${currentConversationId.value}/workspace/import`, { source, kind, branch });
  clearWorkspaceEditorState();
  await refreshWorkspace();
  await loadTerminals();
}

async function switchWorkspaceBranch() {
  if (!currentConversationId.value) return;
  const branches = await apiGet(`/api/conversations/${currentConversationId.value}/workspace/git/branches`);
  const name = window.prompt(`当前：${branches.current}\n可用：${branches.branches.join(', ')}\n输入分支名`);
  if (!name) return;
  const create = !branches.branches.includes(name) && window.confirm("该分支不存在，是否创建？");
  await apiPost(`/api/conversations/${currentConversationId.value}/workspace/git/branches/switch`, { name, create });
  clearWorkspaceEditorState();
  await refreshWorkspace();
}

function clearWorkspaceEditorState() {
  openFiles.value.forEach((tab) => monacoEditor.value?.closeModel(tab.path));
  openFiles.value = [];
  workspaceFilePath.value = "";
  workspaceContent.value = "";
  workspaceOriginal.value = "";
}

async function pollWorkspaceChanges() {
  if (mode.value !== "coding-agent" || !currentConversationId.value || workspaceLoading.value) return;
  try {
    const data = await apiGet(`/api/conversations/${currentConversationId.value}/workspace/files`);
    await applyWorkspaceListing(data, true);
  } catch {
    // A transient poll failure must not disturb editing.
  }
}

async function applyWorkspaceListing(data, syncOpenFiles) {
    workspaceId.value = data.workspace_id || "";
    workspaceEntries.value = data.entries || (data.files || []).map((path) => ({ path, type: "file" }));
    const filePaths = workspaceEntries.value.filter((entry) => entry.type === "file").map((entry) => entry.path);
    const removedTabs = openFiles.value.filter((tab) => !filePaths.includes(tab.path));
    openFiles.value = openFiles.value.filter((tab) => filePaths.includes(tab.path));
    removedTabs.forEach((tab) => monacoEditor.value?.closeModel(tab.path));
    if (workspaceFilePath.value && !filePaths.includes(workspaceFilePath.value)) {
      activateWorkspaceTab(openFiles.value[0]?.path || "");
    }
    if (!syncOpenFiles) return;
    const versions = new Map(workspaceEntries.value.filter((entry) => entry.type === "file").map((entry) => [entry.path, entry.version]));
    await Promise.all(openFiles.value.map(async (tab) => {
      const serverVersion = versions.get(tab.path);
      if (!serverVersion || !tab.version || serverVersion === tab.version) return;
      if (tab.content !== tab.original) {
        tab.conflict = true;
        return;
      }
      const fresh = await apiGet(`/api/conversations/${currentConversationId.value}/workspace/file?path=${encodeURIComponent(tab.path)}`);
      tab.content = fresh.content;
      tab.original = fresh.content;
      tab.version = fresh.version;
      tab.conflict = false;
      if (tab.path === workspaceFilePath.value) {
        workspaceContent.value = fresh.content;
        workspaceOriginal.value = fresh.content;
      }
    }));
}

async function openWorkspaceFile(path, forceReload = false) {
  if (!currentConversationId.value) return;
  const existing = openFiles.value.find((tab) => tab.path === path);
  if (existing && !forceReload) {
    activateWorkspaceTab(path);
    return;
  }
  const data = await apiGet(`/api/conversations/${currentConversationId.value}/workspace/file?path=${encodeURIComponent(path)}`);
  if (existing) {
    existing.content = data.content;
    existing.original = data.content;
    existing.version = data.version;
    existing.conflict = false;
  } else {
    openFiles.value.push({ path: data.path, content: data.content, original: data.content, version: data.version, conflict: false });
  }
  activateWorkspaceTab(data.path);
}

function activateWorkspaceTab(path) {
  const tab = openFiles.value.find((item) => item.path === path);
  workspaceFilePath.value = tab?.path || "";
  selectedWorkspaceDirectory.value = "";
  workspaceContent.value = tab?.content || "";
  workspaceOriginal.value = tab?.original || "";
  workspaceOutput.value = "";
  reviewVisible.value = false;
}

async function closeWorkspaceTab(path, force = false) {
  const tab = openFiles.value.find((item) => item.path === path);
  if (!tab) return;
  if (!force && tab.content !== tab.original && !window.confirm(`关闭“${path}”并放弃未保存修改？`)) return;
  const index = openFiles.value.indexOf(tab);
  openFiles.value.splice(index, 1);
  if (workspaceFilePath.value === path) {
    activateWorkspaceTab(openFiles.value[index]?.path || openFiles.value[index - 1]?.path || "");
  }
  await nextTick();
  monacoEditor.value?.closeModel(path);
}

function handleEditorNavigate({ path, content }) {
  let tab = openFiles.value.find((item) => item.path === path);
  if (!tab) {
    tab = { path, content, original: content };
    openFiles.value.push(tab);
  }
  activateWorkspaceTab(path);
}

function formatWorkspaceFile() {
  monacoEditor.value?.formatDocument();
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
  if (workspaceFilePath.value === path) await openWorkspaceFile(path, true);
}

async function restoreTrashItem(trashId) {
  await apiPost(`/api/conversations/${currentConversationId.value}/workspace/trash/restore`, { trash_id: trashId });
  await refreshWorkspace();
}

async function saveWorkspaceFile() {
  if (!currentConversationId.value || !workspaceFilePath.value || workspaceSaving.value) return;
  workspaceSaving.value = true;
  try {
    const tab = openFiles.value.find((item) => item.path === workspaceFilePath.value);
    const force = Boolean(tab?.conflict && window.confirm("文件已被 Agent 或终端修改。是否用编辑器内容覆盖磁盘版本？"));
    if (tab?.conflict && !force) return;
    const saved = await apiPost(`/api/conversations/${currentConversationId.value}/workspace/file`, {
      path: workspaceFilePath.value,
      content: workspaceContent.value,
      expected_version: tab?.version || null,
      force,
    });
    workspaceOriginal.value = workspaceContent.value;
    if (tab) {
      tab.original = workspaceContent.value;
      tab.version = saved.version;
      tab.conflict = false;
    }
    await refreshWorkspace();
  } catch (error) {
    const tab = openFiles.value.find((item) => item.path === workspaceFilePath.value);
    if (error.status === 409 && tab) tab.conflict = true;
    throw error;
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
  const removedTabs = openFiles.value.filter((tab) => tab.path === entry.path || tab.path.startsWith(`${entry.path}/`));
  openFiles.value = openFiles.value.filter((tab) => !removedTabs.includes(tab));
  removedTabs.forEach((tab) => monacoEditor.value?.closeModel(tab.path));
  if (workspaceFilePath.value === entry.path || workspaceFilePath.value.startsWith(`${entry.path}/`)) {
    activateWorkspaceTab(openFiles.value[0]?.path || "");
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
  const renamed = [];
  openFiles.value.forEach((tab) => {
    if (tab.path === entry.path || tab.path.startsWith(`${entry.path}/`)) {
      const oldPath = tab.path;
      tab.path = target + tab.path.slice(entry.path.length);
      renamed.push(oldPath);
    }
  });
  renamed.forEach((path) => monacoEditor.value?.closeModel(path));
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

function interruptTerminal() {
  terminalView.value?.sendCtrlC();
}

async function scrollTerminal() {
  await nextTick();
  terminalView.value?.fit();
}

function updateActiveTerminal(payload) {
  const index = terminals.value.findIndex((item) => item.id === payload?.id);
  if (index >= 0) terminals.value[index] = { ...terminals.value[index], ...payload };
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
