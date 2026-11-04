"""给 CDK 契约测试用：用 FakeGateway 走一遍完整流程，把生成的部署配置 JSON 打到 stdout。

用法（在 cli/ 目录下）：python3 -m tests.sample_config new|existing
这里的资源 ID 用真实格式（十六进制），因为 CDK 端会校验 ID 格式。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Dict, Sequence, Tuple

from qwenctl.cli import EXIT_OK, main
from qwenctl.gateway import EndpointServices
from qwenctl.network import RouteTable, Subnet, Vpc

from .conftest import FakeGateway, make_console

_SUBNETS = (
    Subnet("subnet-0a0000000000000a1", "apse5-az1", "ap-southeast-5a", "10.0.0.0/24", 250),
    Subnet("subnet-0b0000000000000b1", "apse5-az2", "ap-southeast-5b", "10.0.1.0/24", 250),
    Subnet("subnet-0a0000000000000a2", "apse5-az1", "ap-southeast-5a", "10.0.32.0/19", 8000),
    Subnet("subnet-0b0000000000000b2", "apse5-az2", "ap-southeast-5b", "10.0.64.0/19", 8000),
)
_ROUTE_TABLES = (
    RouteTable(
        "rtb-0f0000000000000f1", ("subnet-0a0000000000000a1", "subnet-0b0000000000000b1"), False, "igw-1"
    ),
    RouteTable("rtb-0a0000000000000a2", ("subnet-0a0000000000000a2",), False, "nat-1"),
    RouteTable("rtb-0b0000000000000b2", ("subnet-0b0000000000000b2",), False, "nat-2"),
)
_GATEWAYS: Dict[str, FakeGateway] = {
    "new": FakeGateway(),
    "existing": FakeGateway(
        vpc_list=(Vpc("vpc-0c0000000000000c1", "10.0.0.0/16", "factory", False, ("100.64.0.0/16",)),),
        subnet_list=_SUBNETS,
        route_list=_ROUTE_TABLES,
        endpoints=EndpointServices(
            interface=frozenset({"sts"}), s3_gateway_route_table_ids=frozenset({"rtb-0a0000000000000a2"})
        ),
    ),
}
# existing：选现有 VPC → 选第 1 个 VPC → 仅内部访问 → 其余默认 → 确认
_ANSWERS: Dict[str, Tuple[str, ...]] = {"new": (), "existing": ("2", "1", "1", "", "", "", "", "", "y")}


def generate(mode: str) -> str:
    answers: Sequence[str] = _ANSWERS[mode]
    console, out = make_console(answers, assume_defaults=(mode == "new"))
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "config.json")
        args = ["deploy", "--plan", "--output", target] + (["--yes"] if mode == "new" else [])
        code = main(args, lambda region: _GATEWAYS[mode], console)
        if code != EXIT_OK:
            raise SystemExit(f"qwenctl 退出码 {code}：\n{out.getvalue()}")
        with open(target, encoding="utf-8") as handle:
            return handle.read()


if __name__ == "__main__":
    sys.stdout.write(json.dumps(json.loads(generate(sys.argv[1])), ensure_ascii=False))
