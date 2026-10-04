import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { UserInfo, LoginRequest, LoginResponse } from '@/types/user'
import { login as loginApi, getUserInfo } from '@/api/auth'
import { clearDingtalkOAuthState } from '@/utils/dingtalk-auth'
import { sso } from '@/auth/session'

export const useUserStore = defineStore('user', () => {
  const token = ref<string>(sso.isSso ? '' : localStorage.getItem('kb_token') || '')
  const userInfo = ref<UserInfo | null>(null)

  function setToken(t: string) {
    token.value = t
    if (!sso.isSso) localStorage.setItem('kb_token', t)
    else localStorage.removeItem('kb_token')
  }

  /** 整体替换用户信息并持久化（传 null 等价于清除缓存） */
  function setUserInfo(u: UserInfo | null) {
    userInfo.value = u
    if (u && !sso.isSso) localStorage.setItem('kb_user', JSON.stringify(u))
    else localStorage.removeItem('kb_user')
  }

  /** 局部更新用户信息：设密成功后清 must_change_password、绑定钉钉后刷新 dingtalk_binding */
  function patchUserInfo(patch: Partial<UserInfo>) {
    if (!userInfo.value) return
    setUserInfo({ ...userInfo.value, ...patch })
  }

  /** 登录结果统一落地（密码登录 / 钉钉扫码 / H5 免登共用） */
  function applyLogin(res: LoginResponse) {
    setToken(res.access_token)
    setUserInfo(res.user)
    return res
  }

  async function login(payload: LoginRequest) {
    const res: LoginResponse = await loginApi(payload)
    applyLogin(res)
    return res
  }

  async function fetchUserInfo() {
    const res = await getUserInfo()
    setUserInfo(res)
    return res
  }

  function logout(endSso = true) {
    token.value = ''
    userInfo.value = null
    localStorage.removeItem('kb_token')
    localStorage.removeItem('kb_user')
    // 未消费完的钉钉扫码 state 一并清掉，避免下次回调误判
    clearDingtalkOAuthState()
    if (endSso && sso.isSso) void sso.logout()
  }

  function restoreUser() {
    if (sso.isSso) return
    const cached = localStorage.getItem('kb_user')
    if (cached) {
      userInfo.value = JSON.parse(cached)
    }
  }

  return {
    token,
    userInfo,
    setToken,
    setUserInfo,
    patchUserInfo,
    applyLogin,
    login,
    fetchUserInfo,
    logout,
    restoreUser,
  }
})
