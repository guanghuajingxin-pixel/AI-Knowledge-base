"""对真实调度中心执行 ensure_group（部署验证用，可重复执行）。"""
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))          # services/kb-api → app 包
sys.path.insert(0, str(_HERE.parent.parent / "kb-common"))

from app.services.sync.xxljob_admin import XxlJobAdminClient  # noqa: E402

c = XxlJobAdminClient(
    "http://10.10.166.2:8088", "admin", "y4bEFkvU0x51jLA9ni",
    "24b53636a8518085ce84837ee453f8d42d9ca771ae1d8da7")
gid = c.ensure_group("xxl-job-executor-kge", "知识治理专家同步执行器")
print("group id:", gid)
page = c._post("/jobgroup/pageList", {"start": 0, "length": 100,
                                      "appname": "xxl-job-executor-kge", "title": ""})
print("rows:", XxlJobAdminClient._rows(page))
