#!/usr/bin/env bash
# 切换人类登录之前，在一次性实例上把「平台 API Key 签发 → 检索 OpenAPI」整条链路跑通。
#
# 为什么用独立实例：AUTH_PROVIDER=keycloak 会同时停掉本地密码和钉钉直登，生产 kb-api
# 不能拿来做实验。这里用同一镜像起临时实例（默认 8015），只额外注入 Keycloak 变量，
# 并关掉 XXL-Job 执行器，避免和正式实例抢同一批调度任务。
#
# 两个必须绕开的 Keycloak 语义（踩过）：
#   1. bootstrap-admin 是「临时管理员」，只接受来自容器 loopback 的调用，
#      所以管理令牌只能在容器内从 kcadm 缓存里取。
#   2. 带 UPDATE_PASSWORD 的一次性密码不能走非浏览器的 password grant
#      （报 "Account is not fully set up"）。验证期间临时改用非一次性密码，
#      结束后恢复一次性密码 + UPDATE_PASSWORD，并重新写入密码文件。
#
# 在身份主机上执行：bash verify-platform-keys.sh        （跑完自动清理）
#                  KEEP=1 bash verify-platform-keys.sh  （保留实例便于继续手测）
set -uo pipefail

DIR=${DIR:-/opt/kge/deploy/intranet}
KCDIR=${KCDIR:-/opt/kge/deploy/keycloak}
STAGE_FILE=$DIR/docker-compose.keycloak-verify.yml
SERVICE=kb-api-verify
STAGE_CONTAINER=kge-kb-api-verify
ISSUER=${ISSUER:-http://127.0.0.1:8180/realms/jack}
# 容器内可达的身份中心地址：issuer 仍按上面的字符串校验，只把网络出口换成服务名
INTERNAL=${INTERNAL:-http://keycloak:8080}
PORT=${PORT:-8015}
REALM_PATH=/realms/jack/protocol/openid-connect
WEB_CLIENT=${WEB_CLIENT:-knowledge-web}
RUN_USER=${RUN_USER:-admin}
KEEP=${KEEP:-0}
KCONTAINER=jack-identity-keycloak-1
VENV=/app/services/kb-api/.venv/bin
ENVF=$KCDIR/.env
MAP=$KCDIR/account-map.json
PWF=$KCDIR/initial-passwords.env
PG_CONTAINER=${PG_CONTAINER:-kge-postgres}
PG_USER=${PG_USER:-dev}
PG_DB=${PG_DB:-dev_db}
TMP_PW=/tmp/kc-verify-runner.pwd

results=()
note() { results+=("$1|$2"); printf '%-56s %s\n' "$2" "$1"; }
kc() { docker exec -i "$KCONTAINER" /opt/keycloak/bin/kcadm.sh "$@" -r jack --server http://localhost:8080 2>&1; }
envv() { sed -n "s/^$1=//p" "$2" | head -1; }
map_get() { python3 -c 'import json,sys
print(json.load(open("'"$MAP"'")).get(sys.argv[1],{}).get(sys.argv[2],""))' "$1" "$2"; }
new_pw() { python3 -c 'import secrets,string
abc = string.ascii_uppercase*2 + string.ascii_lowercase*3 + string.digits*2 + "!@#%*"
while True:
    v = "".join(secrets.choice(abc) for _ in range(14))
    if any(c.isupper() for c in v) and any(c.islower() for c in v) and any(c.isdigit() for c in v):
        print(v); break'; }
kc_login() {
  docker exec -i "$KCONTAINER" /opt/keycloak/bin/kcadm.sh config credentials \
    --server http://localhost:8080 --realm master \
    --user "$(envv KEYCLOAK_ADMIN "$ENVF")" --password "$(envv KEYCLOAK_ADMIN_PASSWORD "$ENVF")" >/dev/null 2>&1; }
client_uuid() { kc get clients --fields id,clientId | python3 -c 'import json,sys
print(next((c["id"] for c in json.load(sys.stdin) if c.get("clientId")==sys.argv[1]),""))' "$1"; }

# kcadm 没有清除 requiredActions 的开关（-d 只删请求体字段），只能回写整个表示。
set_required_actions() {
  local id=$1 ra=$2
  kc get "users/$id" | python3 -c 'import json,sys
user = json.load(sys.stdin)
user["requiredActions"] = [r for r in sys.argv[2].split(",") if r.strip()]
for key in ("access", "notBefore", "attributes", "origin", "clientRoles", "groups",
            "realmRoles", "federatedIdentities", "credentials",
            "disableableCredentialTypes", "requiredActionPrivileges"):
    user.pop(key, None)
print(json.dumps(user, ensure_ascii=False))' "$id" "$ra" \
    | docker exec -i "$KCONTAINER" /opt/keycloak/bin/kcadm.sh update "users/$id" -f - \
        -r jack --server http://localhost:8080 2>&1
}

required_actions() {
  kc get "users/$1" --fields requiredActions | python3 -c 'import json,sys
print(",".join(json.load(sys.stdin).get("requiredActions") or []))'
}

cleanup_done=0
cleanup() {
  [ "$cleanup_done" = 1 ] && return
  cleanup_done=1
  local web_id su restore
  web_id=$(client_uuid "$WEB_CLIENT")
  [ -n "$web_id" ] && kc update "clients/$web_id" -s directAccessGrantsEnabled=false >/dev/null 2>&1
  kc_login
  su=$(map_get "$RUN_USER" kc_id)
  if [ -n "$su" ]; then
    # 恢复一次性密码 + 首次登录强制改密，不给生产留一个可长期密码直连的账号
    restore=$(new_pw)
    kc set-password --userid "$su" -t -p "$restore" >/dev/null 2>&1
    set_required_actions "$su" "UPDATE_PASSWORD" >/dev/null
    python3 - "$PWF" "$RUN_USER" "$restore" <<'PY'
import sys
path, user, password = sys.argv[1], sys.argv[2], sys.argv[3]
key = f'KC_PASSWORD_{user}='
lines = [line for line in open(path, encoding='utf-8').read().splitlines() if not line.startswith(key)]
lines.append(key + password)
open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
PY
    chmod 600 "$PWF"
    echo "已恢复 $RUN_USER 的一次性密码（新值写入 $PWF）并关闭 knowledge-web 密码直连"
  fi
  if [ "$KEEP" != 1 ]; then
    docker compose -f "$DIR/docker-compose.app.yml" -f "$STAGE_FILE" stop "$SERVICE" >/dev/null 2>&1
    docker container prune -f >/dev/null 2>&1
    echo "临时实例已停止；compose 文件保留：$STAGE_FILE"
  fi
}
trap cleanup EXIT

[ -f "$MAP" ] || { echo "缺少 $MAP，请先跑 provision-accounts.py" >&2; exit 1; }
LOCAL_ID=$(map_get "$RUN_USER" local_id); SUBJECT=$(map_get "$RUN_USER" kc_id)
[ -n "$SUBJECT" ] || { echo "$MAP 里没有 $RUN_USER" >&2; exit 1; }

echo '=== 0. 联调准备：临时允许密码直连，并改用非一次性密码 ==='
kc_login || { echo "无法登录身份中心" >&2; exit 1; }
WEB_ID=$(client_uuid "$WEB_CLIENT")
[ -n "$WEB_ID" ] || { echo "找不到客户端 $WEB_CLIENT" >&2; exit 1; }
kc update "clients/$WEB_ID" -s directAccessGrantsEnabled=true >/dev/null
set_required_actions "$SUBJECT" "" >/dev/null
RUN_PW=$(new_pw); printf '%s' "$RUN_PW" > "$TMP_PW"; chmod 600 "$TMP_PW"
# 非一次性密码（不带 -t），否则非浏览器流程会被 UPDATE_PASSWORD 卡住
kc set-password --userid "$SUBJECT" -p "$RUN_PW" >/dev/null
left=$(required_actions "$SUBJECT")
[ -z "$left" ] || { echo "requiredActions 未能清空（剩余：$left）" >&2; exit 1; }

echo '=== 1. 起临时实例（同镜像，只多注入 Keycloak 变量）==='
cat > "$STAGE_FILE" <<EOF
# verify-platform-keys.sh 生成：仅用于联调验证，不参与正式 up -d
services:
  $SERVICE:
    image: kge-kb-api:latest
    container_name: $STAGE_CONTAINER
    restart: "no"
    ports:
      - "$PORT:$PORT"
    env_file:
      - .env.app
      - platform-keys.env
    environment:
      AUTH_PROVIDER: keycloak
      OIDC_ISSUER: $ISSUER
      OIDC_JWKS_URL: $INTERNAL$REALM_PATH/certs
      OIDC_TOKEN_URL: $INTERNAL$REALM_PATH/token
      OIDC_ADMIN_URL: http://keycloak:8080/admin/realms/jack
      # 不注册执行器，避免和正式 kb-api 抢同一批调度任务
      XXL_JOB_ENABLED: "false"
    command: $VENV/uvicorn app.main:app --host 0.0.0.0 --port $PORT
    networks: [kge-network, identity-net]
networks:
  kge-network:
    external: true
    name: kge-network
  identity-net:
    external: true
    name: jack-identity_default
EOF
docker compose -f "$DIR/docker-compose.app.yml" -f "$STAGE_FILE" up -d --no-deps "$SERVICE" >/dev/null
# 这台老机器上 kb-api 冷启动约 1-2 分钟（还要连钉钉 stream），耐心等
ready=0
for _ in $(seq 1 100); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/v1/auth/config")
  [ "$code" = 200 ] && { ready=1; break; }
  sleep 3
done
[ "$ready" = 1 ] || { echo "临时实例没起来，日志：" >&2; docker logs --tail 30 "$STAGE_CONTAINER" >&2; exit 1; }
config=$(curl -s "http://127.0.0.1:$PORT/api/v1/auth/config")
case "$config" in
  *'"provider":"keycloak"'*) note PASS 'auth/config 报告 keycloak 模式';;
  *) note FAIL "auth/config: $config";;
esac

echo '=== 2. 身份绑定：Keycloak 账号 <-> 业务账号（业务 ID 与知识归属不变）==='
# 正式切换时 issuer 是公网 HTTPS 地址，直接跑 scripts/keycloak_accounts.py 即可；
# 这里 issuer 指向 loopback，容器内不可达，所以做等价的 SQL 绑定（先校验用户名一致）。
REMOTE_USER=$(kc get "users/$SUBJECT" --fields id,username | python3 -c 'import json,sys
d = json.load(sys.stdin)
print(d.get("username","") if d.get("id")==sys.argv[1] else "")' "$SUBJECT")
if [ "$REMOTE_USER" != "$RUN_USER" ]; then
  note FAIL "身份中心 $SUBJECT 用户名是 '$REMOTE_USER'，与业务账号 $RUN_USER 不一致，拒绝绑定"
  exit 1
fi
docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -Atc \
  "insert into user_identities(id,user_id,issuer,subject)
   select gen_random_uuid(), '$LOCAL_ID'::uuid, '$ISSUER', '$SUBJECT'
   where not exists (select 1 from user_identities
                     where issuer='$ISSUER' and subject='$SUBJECT')" >/dev/null 2>&1
mapped=$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atc \
  "select count(*) from user_identities where issuer='$ISSUER' and subject='$SUBJECT' and user_id='$LOCAL_ID'::uuid")
[ "$mapped" = "1" ] && note PASS "已绑定 $RUN_USER（业务 ID $LOCAL_ID 保持不变）" \
  || note FAIL '绑定后仍查不到 user_identities 映射'

echo '=== 3. 统一身份登录 → 平台 API Key 接口（页面用的就是这两个）==='
GRANT=$(curl -s -X POST "http://127.0.0.1:8180$REALM_PATH/token" -d 'grant_type=password' \
  -d "client_id=$WEB_CLIENT" -d "username=$RUN_USER" --data-urlencode "password=$RUN_PW")
USER_TOKEN=$(python3 -c 'import json,sys
print(json.loads(sys.argv[1]).get("access_token",""))' "$GRANT")
GRANT_ERROR=$(python3 -c 'import json,sys
d = json.loads(sys.argv[1])
print(d.get("error_description") or d.get("error") or "")' "$GRANT")
RAW=''; KEY_ID=''
if [ -n "$USER_TOKEN" ]; then
  note PASS '统一身份登录取令牌（kb-api 客户端角色生效）'
  list_code=$(curl -s -o /tmp/kc-list.json -w '%{http_code}' -H "Authorization: Bearer $USER_TOKEN" \
    "http://127.0.0.1:$PORT/api/v1/platform/api-keys")
  if [ "$list_code" = 200 ]; then
    count=$(python3 -c 'import json;print(len(json.load(open("/tmp/kc-list.json"))))' 2>/dev/null)
    note PASS "GET /platform/api-keys -> 200（$count 个受管应用）"
  else
    note FAIL "GET /platform/api-keys -> $list_code $(head -c 200 /tmp/kc-list.json)"
  fi
  create=$(curl -s -X POST -H "Authorization: Bearer $USER_TOKEN" -H 'Content-Type: application/json' \
    -d '{"name":"身份中心联调验证（脚本自动删除）"}' "http://127.0.0.1:$PORT/api/v1/platform/api-keys")
  RAW=$(python3 -c 'import json,sys
print(json.loads(sys.argv[1]).get("raw_key",""))' "$create")
  KEY_ID=$(python3 -c 'import json,sys
print(json.loads(sys.argv[1]).get("id",""))' "$create")
  if [ -n "$RAW" ]; then
    note PASS 'POST /platform/api-keys 签发成功（返回 client_id:secret）'
    reveal=$(curl -s -H "Authorization: Bearer $USER_TOKEN" \
      "http://127.0.0.1:$PORT/api/v1/platform/api-keys/$KEY_ID/secret")
    same=$(python3 -c 'import json,sys
print(json.loads(sys.argv[1]).get("raw_key","")==sys.argv[2])' "$reveal" "$RAW")
    [ "$same" = True ] && note PASS '回显接口取回同一密钥' \
      || note FAIL "回显接口异常: $(echo "$reveal" | head -c 120)"
  else
    note FAIL "POST /platform/api-keys 失败: $(echo "$create" | head -c 200)"
  fi
else
  note FAIL "统一身份登录取令牌失败：${GRANT_ERROR:-未知}（见脚本头部“一次性密码”说明）"
fi

echo '=== 4. 用签发的 Key 调知识库检索 OpenAPI ==='
if [ -n "$RAW" ]; then
  api=$(curl -s -o /tmp/kc-api.json -w '%{http_code}' -X POST -H "X-API-Key: $RAW" \
    -H 'Content-Type: application/json' \
    -d '{"query":"入职培训","library_ids":[6,7,8],"top_k":3,"mode":"hybrid"}' \
    "http://127.0.0.1:$PORT/api/openapi/v1/knowledge/retrieve")
  total=$(python3 -c 'import json;print(json.load(open("/tmp/kc-api.json")).get("total","?"))' 2>/dev/null)
  [ "$api" = 200 ] && note PASS "OpenAPI 检索 -> 200，返回 $total 条" \
    || note FAIL "OpenAPI 检索 -> $api $(head -c 220 /tmp/kc-api.json)"
  bad=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'X-API-Key: platform-api-deadbeef:nope' \
    -H 'Content-Type: application/json' -d '{"query":"x","library_ids":[6]}' \
    "http://127.0.0.1:$PORT/api/openapi/v1/knowledge/retrieve")
  [ "$bad" = 401 ] && note PASS '伪造 Key 被拒（401）' || note FAIL "伪造 Key -> $bad（期望 401）"

  echo '=== 5. 页面停用即失效（集中吊销语义）==='
  patch_code=$(curl -s -o /tmp/kc-patch.json -w '%{http_code}' -X PATCH -H "Authorization: Bearer $USER_TOKEN" \
    -H 'Content-Type: application/json' -d '{"enabled":false}' \
    "http://127.0.0.1:$PORT/api/v1/platform/api-keys/$KEY_ID")
  [ "$patch_code" = 200 ] || note FAIL "停用接口 -> $patch_code $(head -c 160 /tmp/kc-patch.json)"
  off=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-API-Key: $RAW" \
    -H 'Content-Type: application/json' -d '{"query":"入职培训","library_ids":[6],"top_k":1}' \
    "http://127.0.0.1:$PORT/api/openapi/v1/knowledge/retrieve")
  [ "$off" = 401 ] && note PASS '停用后 OpenAPI -> 401' || note FAIL "停用后 OpenAPI -> $off（期望 401）"
  curl -s -o /dev/null -X DELETE -H "Authorization: Bearer $USER_TOKEN" \
    "http://127.0.0.1:$PORT/api/v1/platform/api-keys/$KEY_ID"
  note INFO '验证用平台应用已删除，不留在生产 realm 里'
fi

echo
echo '=== 结果 ==='
printf '%s\n' "${results[@]}" | sed 's/|/ :: /'
fails=$(printf '%s\n' "${results[@]}" | grep -c '^FAIL' || true)
if [ "$fails" = 0 ]; then
  echo '全部通过：平台 API Key 链路可用，可以按 README 的切换步骤动生产。'
else
  echo "有 $fails 项失败，先别切生产。"
fi
exit "$fails"
