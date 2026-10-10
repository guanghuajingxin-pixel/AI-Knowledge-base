#!/usr/bin/env bash
# Provision the realm objects the platform API Key page needs, from inside the
# Keycloak container (the bootstrap admin token is only accepted from loopback).
#
#   1. client role kb-api/knowledge:retrieve   (what a platform key is granted)
#   2. confidential client platform-key-manager (kb-api OIDC_ADMIN_CLIENT_ID)
#   3. realm-management roles for its service account (manage client lifecycle)
#
# Idempotent: existing objects are reused, secrets are never overwritten.
# Usage (on the identity host):  bash provision-platform-keys.sh
set -euo pipefail

REALM=${REALM:-jack}
CONTAINER=${CONTAINER:-jack-identity-keycloak-1}
ENV_FILE=${ENV_FILE:-/opt/kge/deploy/keycloak/.env}
ADMIN_CLIENT=${ADMIN_CLIENT:-platform-key-manager}
MANAGEMENT_ROLES="manage-clients,view-clients,query-clients,manage-users,view-users,query-users"

env_value() { sed -n "s/^$1=//p" "$ENV_FILE" | head -1; }
KC_ADMIN_USER=$(env_value KEYCLOAK_ADMIN)
KC_ADMIN_PASS=$(env_value KEYCLOAK_ADMIN_PASSWORD)
[ -n "$KC_ADMIN_USER" ] && [ -n "$KC_ADMIN_PASS" ] || { echo "缺少 bootstrap 管理员凭据：$ENV_FILE" >&2; exit 1; }

adm() { docker exec -i "$CONTAINER" /opt/keycloak/bin/kcadm.sh "$@" -r "$REALM" \
          --server http://localhost:8080 2>&1; }
# Occasional empty responses from kcadm over docker exec; retry until JSON arrives.
adm_json() {
  local out
  for _ in 1 2 3 4 5; do
    out=$(adm "$@")
    if printf '%s' "$out" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
      printf '%s' "$out"; return 0
    fi
    sleep 2
  done
  printf '%s' "$out"; return 1
}
login() { docker exec -i "$CONTAINER" /opt/keycloak/bin/kcadm.sh config credentials \
            --server http://localhost:8080 --realm master \
            --user "$KC_ADMIN_USER" --password "$KC_ADMIN_PASS" >/dev/null 2>&1; }

# kcadm's [?clientId?] shortcut returns nothing on 26.7, so filter locally.
client_uuid() {
  adm_json get clients --fields id,clientId \
    | python3 -c 'import json,sys
rows = json.load(sys.stdin)
row = next((r for r in rows if r.get("clientId") == sys.argv[1]), None)
print(row["id"] if row else "")' "$1"
}

field() { python3 -c 'import json,sys
data = json.load(sys.stdin)
print(data.get(sys.argv[1], "") if isinstance(data, dict) else "")' "$1"; }

login

KB_ID=$(client_uuid kb-api)
[ -n "$KB_ID" ] || { echo "未找到资源客户端 kb-api，请检查 realm 导入" >&2; exit 1; }

# 0. 新版 Keycloak 的 sub 声明由 basic 客户端 scope 提供；缺了它，access token
#    没有 sub，kb-api 的 require_sub 会把统一登录判成 401（realm import 老文件里没带）。
BASIC_SCOPE=$(adm_json get client-scopes --fields id,name \
  | python3 -c 'import json,sys;print(next((x["id"] for x in json.load(sys.stdin) if x.get("name")=="basic"),""))')
[ -n "$BASIC_SCOPE" ] || { echo "realm 里没有 basic 客户端 scope" >&2; exit 1; }
ADM_TOKEN=$(docker exec "$CONTAINER" cat /opt/keycloak/.keycloak/kcadm.config \
  | python3 -c 'import json,sys;c=json.load(sys.stdin);e=next(iter(c["endpoints"].values()));print(next(iter(e.values()))["token"])')
for web_client in jack-portal knowledge-web; do
  CID=$(client_uuid "$web_client")
  [ -n "$CID" ] || { echo "缺少公开客户端 $web_client" >&2; exit 1; }
  docker exec -i "$CONTAINER" curl -s -o /dev/null -w "  $web_client 挂载 basic scope: %{http_code}\n" \
    -X PUT -H "Authorization: Bearer $ADM_TOKEN" \
    "http://localhost:8080/admin/realms/jack/clients/$CID/default-client-scopes/$BASIC_SCOPE"
done

# 1. retrieval role
if adm_json get "clients/$KB_ID/roles" | python3 -c 'import json,sys
sys.exit(0 if any(r.get("name") == "knowledge:retrieve" for r in json.load(sys.stdin)) else 1)'; then
  echo "角色 kb-api/knowledge:retrieve 已存在"
else
  adm create "clients/$KB_ID/roles" -s name=knowledge:retrieve -s composite=false \
    -s description=知识库检索 >/dev/null
  adm_json get "clients/$KB_ID/roles" | python3 -c 'import json,sys
sys.exit(0 if any(r.get("name") == "knowledge:retrieve" for r in json.load(sys.stdin)) else 1)' \
    || { echo "创建 knowledge:retrieve 失败" >&2; exit 1; }
  echo "已创建客户端角色 kb-api/knowledge:retrieve"
fi

# 2. management service client
MG_ID=$(client_uuid "$ADMIN_CLIENT")
if [ -n "$MG_ID" ]; then
  echo "管理客户端 $ADMIN_CLIENT 已存在，保留其密钥与授权"
else
  adm create clients -s clientId="$ADMIN_CLIENT" -s name='平台密钥管理服务' \
    -s protocol=openid-connect -s enabled=true -s publicClient=false \
    -s clientAuthenticatorType=client-secret -s serviceAccountsEnabled=true \
    -s standardFlowEnabled=false -s implicitFlowEnabled=false \
    -s directAccessGrantsEnabled=false -s fullScopeAllowed=true >/dev/null
  MG_ID=$(client_uuid "$ADMIN_CLIENT")
  [ -n "$MG_ID" ] || { echo "创建管理客户端 $ADMIN_CLIENT 失败" >&2; exit 1; }
  echo "已创建管理客户端 $ADMIN_CLIENT"
fi

# 3. service account + realm-management roles
SU_ID=$(adm_json get "clients/$MG_ID/service-account-user" | field id)
[ -n "$SU_ID" ] || { echo "无法读取 $ADMIN_CLIENT 的服务账号" >&2; exit 1; }
# KC 26 的 add-roles 只认 --uid/--cclientid/--rolename（旧 --uinfo/--ccclient/--ccl-roles 已移除）。
IFS=',' read -r -a role_list <<< "$MANAGEMENT_ROLES"
role_args=()
for role in "${role_list[@]}"; do role_args+=(--rolename "$role"); done
adm add-roles --uid "$SU_ID" --cclientid realm-management "${role_args[@]}" >/dev/null
echo "服务账号已授予 realm-management 角色：$MANAGEMENT_ROLES"

SECRET=$(adm_json get "clients/$MG_ID/client-secret" | field value)
[ -n "$SECRET" ] || { echo "读取管理客户端密钥失败" >&2; exit 1; }

# Hand the pair to the app env file without printing the secret.
OUT=${OUT:-/opt/kge/deploy/intranet/platform-keys.env}
umask 077
cat > "$OUT" <<EOF
# 由 provision-platform-keys.sh 生成：kb-api 签发平台 API Key 用的身份中心管理服务账号。
# 合并进 /opt/kge/deploy/intranet/.env.app 后重启 kb-api。
OIDC_ADMIN_CLIENT_ID=$ADMIN_CLIENT
OIDC_ADMIN_CLIENT_SECRET=$SECRET
EOF
echo "已写入 $OUT（权限 600，未回显密钥）"
