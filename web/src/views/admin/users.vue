<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, ArrowDown } from '@element-plus/icons-vue'
import PageContainer from '@/components/common/PageContainer.vue'
import { getUsers, createUser, updateUser, deleteUser, unbindDingtalk } from '@/api/users'
import type { UserInfo, UserRole } from '@/types/user'
import { formatDate } from '@/utils/format'
import { authConfig } from '@/auth/session'
import UnifiedAccountPanel from '@/components/common/unified-account-panel.vue'

const users = ref<UserInfo[]>([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const size = ref(20)

const roleOptions: { value: UserRole; label: string }[] = [
  { value: 'super_admin', label: '超级管理员' },
  { value: 'admin', label: '管理员' },
  { value: 'editor', label: '编辑者' },
  { value: 'viewer', label: '查看者' },
]

/** 角色是分类不是状态：统一灰色 tag，不做多色区分 */
function roleLabel(role: UserRole) {
  return roleOptions.find((o) => o.value === role)?.label ?? role
}

async function fetchData() {
  loading.value = true
  try {
    // include_binding=true：每行附钉钉绑定（姓名 / dt_userid / 绑定时间）
    const res = await getUsers(page.value, size.value, true)
    users.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

function handlePageChange(p: number) {
  page.value = p
  fetchData()
}
function handleSizeChange(s: number) {
  size.value = s
  page.value = 1
  fetchData()
}

// 新建/编辑共用 dialog
const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const form = ref<{
  id?: string
  username: string
  password: string
  email: string
  role: UserRole
  is_active: boolean
}>({ username: '', password: '', email: '', role: 'viewer', is_active: true })

function openCreate() {
  dialogMode.value = 'create'
  form.value = { username: '', password: '', email: '', role: 'viewer', is_active: true }
  dialogVisible.value = true
}

function openEdit(row: UserInfo) {
  dialogMode.value = 'edit'
  form.value = {
    id: row.id,
    username: row.username,
    password: '',
    email: row.email || '',
    role: row.role,
    is_active: row.is_active,
  }
  dialogVisible.value = true
}

async function handleSubmit() {
  if (dialogMode.value === 'create') {
    if (!form.value.username.trim() || !form.value.password.trim()) {
      ElMessage.warning('用户名和密码不能为空')
      return
    }
    await createUser({
      username: form.value.username.trim(),
      password: form.value.password,
      email: form.value.email || undefined,
      role: form.value.role,
    })
    ElMessage.success('用户已创建')
  } else {
    const payload: Record<string, unknown> = {
      email: form.value.email || null,
      role: form.value.role,
      is_active: form.value.is_active,
    }
    if (form.value.password) payload.password = form.value.password
    await updateUser(form.value.id!, payload as Parameters<typeof updateUser>[1])
    ElMessage.success('用户已更新')
  }
  dialogVisible.value = false
  fetchData()
}

// ===== 重置密码（可随机生成 + 强制下次登录改密） =====
const resetDialog = ref(false)
const resetSaving = ref(false)
const resetForm = ref<{ id: string; username: string; password: string; must_change_password: boolean }>({
  id: '', username: '', password: '', must_change_password: true,
})

/** 随机生成 12 位密码（必含大小写字母与数字） */
function generateRandomPassword() {
  const upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
  const lower = 'abcdefghjkmnpqrstuvwxyz'
  const digits = '23456789'
  const all = upper + lower + digits
  const pick = (set: string) => {
    const buf = new Uint32Array(1)
    crypto.getRandomValues(buf)
    return set[buf[0] % set.length]
  }
  const chars = [pick(upper), pick(lower), pick(digits)]
  for (let i = chars.length; i < 12; i++) chars.push(pick(all))
  // 洗牌，避免前三位固定为「大写+小写+数字」的模式
  for (let i = chars.length - 1; i > 0; i--) {
    const buf = new Uint32Array(1)
    crypto.getRandomValues(buf)
    const j = buf[0] % (i + 1)
    ;[chars[i], chars[j]] = [chars[j], chars[i]]
  }
  return chars.join('')
}

function openReset(row: UserInfo) {
  resetForm.value = { id: row.id, username: row.username, password: '', must_change_password: true }
  resetDialog.value = true
}

async function submitReset() {
  const pwd = resetForm.value.password
  if (pwd.length < 8 || !/[A-Za-z]/.test(pwd) || !/[0-9]/.test(pwd)) {
    ElMessage.warning('密码至少 8 位，且须包含字母和数字（可点击「随机生成」）')
    return
  }
  resetSaving.value = true
  try {
    await updateUser(resetForm.value.id, {
      password: pwd,
      must_change_password: resetForm.value.must_change_password,
    })
    ElMessage.success('密码已重置')
    resetDialog.value = false
    fetchData()
  } finally {
    resetSaving.value = false
  }
}

// ===== 解绑钉钉（危险操作：二次确认） =====
async function handleUnbind(row: UserInfo) {
  await ElMessageBox.confirm(
    `确认解绑用户「${row.username}」的钉钉身份？解绑后该用户将失去钉钉权限映射，其同步任务会回退到全局服务账号。`,
    '解绑钉钉',
    { type: 'warning', confirmButtonText: '确认解绑', cancelButtonText: '取消' },
  )
  try {
    await unbindDingtalk(row.id)
    ElMessage.success('已解绑钉钉')
    fetchData()
  } catch {
    // 失败原因（如最后一名 super_admin 返回 409）由响应拦截器统一提示
  }
}

// ===== 停用 / 启用 =====
async function handleToggleActive(row: UserInfo) {
  const action = row.is_active ? '停用' : '启用'
  await ElMessageBox.confirm(`确认${action}用户「${row.username}」？`, `${action}用户`, { type: 'warning' })
  await updateUser(row.id, { is_active: !row.is_active })
  ElMessage.success(`已${action}`)
  fetchData()
}

async function handleDelete(row: UserInfo) {
  await ElMessageBox.confirm(`确认删除用户「${row.username}」？删除后无法恢复。`, '警告', { type: 'warning' })
  await deleteUser(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

// 「更多」下拉命令分发（解绑钉钉 / 停用启用 / 删除）
function handleMoreCommand(command: string, row: UserInfo) {
  if (command === 'unbind') handleUnbind(row)
  else if (command === 'toggle') handleToggleActive(row)
  else if (command === 'delete') handleDelete(row)
}

onMounted(() => { if (authConfig.value?.provider !== 'keycloak') void fetchData() })
</script>

<template>
  <UnifiedAccountPanel v-if="authConfig?.provider === 'keycloak'" administration />
  <PageContainer v-else title="用户管理">
    <template #actions>
      <el-button type="primary" :icon="Plus" @click="openCreate">新建用户</el-button>
    </template>
    <el-table :data="users" v-loading="loading" style="width: 100%">
      <el-table-column prop="username" label="用户名" min-width="140" show-overflow-tooltip />
      <el-table-column prop="email" label="邮箱" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ row.email || '—' }}</template>
      </el-table-column>
      <el-table-column label="钉钉姓名" min-width="120" show-overflow-tooltip>
        <template #default="{ row }">{{ (row as UserInfo).dingtalk_binding?.dt_name || '—' }}</template>
      </el-table-column>
      <el-table-column label="dt_userid" width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ (row as UserInfo).dingtalk_binding?.dt_userid || '—' }}</template>
      </el-table-column>
      <el-table-column label="角色" width="110">
        <template #default="{ row }">
          <el-tag type="info" size="small">{{ roleLabel(row.role as UserRole) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.is_active ? 'success' : 'info'" size="small">{{ row.is_active ? '启用' : '禁用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="强制改密" width="100">
        <template #default="{ row }">
          <el-tag :type="row.must_change_password ? 'warning' : 'info'" size="small">
            {{ row.must_change_password ? '待改密' : '正常' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="绑定时间" width="170">
        <template #default="{ row }">
          {{ (row as UserInfo).dingtalk_binding?.bound_at ? formatDate((row as UserInfo).dingtalk_binding!.bound_at) : '—' }}
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="220" fixed="right">
        <template #default="{ row }">
          <div class="ops">
            <el-button link type="primary" size="small" @click="openEdit(row as UserInfo)">编辑</el-button>
            <el-button link type="primary" size="small" @click="openReset(row as UserInfo)">重置密码</el-button>
            <el-dropdown trigger="click" @command="(cmd: string | number | object) => handleMoreCommand(String(cmd), row as UserInfo)">
              <el-button link type="primary" size="small" aria-label="更多操作">
                更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item
                    command="unbind"
                    divided
                    :disabled="!(row as UserInfo).dingtalk_binding"
                    style="color: var(--el-color-danger)"
                  >
                    解绑钉钉
                  </el-dropdown-item>
                  <el-dropdown-item command="toggle">{{ row.is_active ? '停用账号' : '启用账号' }}</el-dropdown-item>
                  <el-dropdown-item command="delete" style="color: var(--el-color-danger)">删除用户</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      style="margin-top: 16px; justify-content: flex-end;"
      :current-page="page"
      :page-size="size"
      :total="total"
      :page-sizes="[10, 20, 50]"
      layout="total, sizes, prev, pager, next"
      @current-change="handlePageChange"
      @size-change="handleSizeChange"
    />

    <el-dialog v-model="dialogVisible" :title="dialogMode === 'create' ? '新建用户' : '编辑用户'" width="480px">
      <el-form label-width="80px">
        <el-form-item label="用户名">
          <el-input v-model="form.username" :disabled="dialogMode === 'edit'" placeholder="登录用户名" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="form.password" type="password" show-password
                    :placeholder="dialogMode === 'edit' ? '留空则不修改' : '登录密码'" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="form.email" placeholder="可选" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width: 100%;">
            <el-option v-for="r in roleOptions" :key="r.value" :label="r.label" :value="r.value" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="dialogMode === 'edit'" label="状态">
          <el-switch v-model="form.is_active" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSubmit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码弹窗 -->
    <el-dialog v-model="resetDialog" :title="`重置密码 · ${resetForm.username}`" width="480px">
      <el-form label-width="130px" @submit.prevent>
        <el-form-item label="新密码" required>
          <div class="reset-pwd-row">
            <el-input v-model="resetForm.password" type="password" show-password placeholder="至少 8 位，须含字母和数字" />
            <el-button link type="primary" @click="resetForm.password = generateRandomPassword()">随机生成</el-button>
          </div>
          <div class="form-hint">可点击「随机生成」得到 12 位随机密码（含大小写字母和数字），生成后请复制告知用户。</div>
        </el-form-item>
        <el-form-item label="强制下次登录改密">
          <el-switch v-model="resetForm.must_change_password" />
          <span class="form-hint" style="margin-left: 12px">开启后用户下次登录必须先设置新密码</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetDialog = false">取消</el-button>
        <el-button type="primary" :loading="resetSaving" @click="submitReset">确定</el-button>
      </template>
    </el-dialog>
  </PageContainer>
</template>

<style scoped>
.reset-pwd-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}
.reset-pwd-row .el-input {
  flex: 1;
}
.form-hint {
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
  margin-top: 4px;
}
/* 操作列：按钮间距交给 flex gap，避免 el-dropdown 包裹层打断 EP 相邻按钮 margin */
.ops {
  display: flex;
  align-items: center;
  gap: 12px;
}
.ops .el-button + .el-button {
  margin-left: 0;
}
.ops .el-dropdown {
  vertical-align: middle;
}
</style>
