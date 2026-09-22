"""每模型运行时覆盖(resident / gpu / vram_budget)的 Postgres 表。

数据加载统一(2026-06-16,用户拍「拆数据表」):运行时覆盖从 gitignore 的
runtime_overrides.json 文件迁到关系库 —— 拆成正经 typed 列(非 jsonb 文件搬家),
与服务/key/用量同库一处。静态定义仍在 models.yaml;此表只存"每机运行时调整"。

列语义:NULL = 未覆盖(回退 models.yaml);非 NULL = 显式覆盖(含 resident=False、gpu=0
这类有效值,故用 nullable 区分"没设"与"设成 False/0")。vram_budget 拆成 mode + value 两列
(mode=auto 时 value 可 NULL)。gpus 是三态(NULL / [] / [0,2]),见该列注释。
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from src.models.database import Base


class ModelRuntimeOverride(Base):
    __tablename__ = "model_runtime_overrides"

    model_id = Column(String(200), primary_key=True)
    resident = Column(Boolean, nullable=True)
    gpu = Column(Integer, nullable=True)
    # GPU 组(张量并行):`[0, 2]` = 这俩卡当一个单元用,tp=2。与 gpu 并存且**优先**。
    # 三态,别退化成两态:
    #   NULL  = 未覆盖 → 回退 models.yaml 的 `gpus:`
    #   []    = **显式清空组**(用户点了单卡)→ 覆盖掉 yaml 的组,按单卡 gpu 走
    #   [0,2] = 覆盖成这个组
    # 没有 `[]` 这个哨兵的话,YAML 里配了 gpus 的模型永远退不出组:PATCH ?gpu=N 只写
    # gpu 列、gpus 仍是 NULL → 合并时回退 YAML 的组 → 单卡设置表面写了实际没生效。
    # JSONB 而非 typed 多列:组大小可变(2/4/8 卡)。
    gpus = Column(JSONB, nullable=True)
    vram_budget_mode = Column(String(20), nullable=True)   # auto | percent | absolute
    vram_budget_value = Column(Float, nullable=True)
    # 启动参数覆盖(spec 2026-09-22)。只存**被覆盖的键**,其余回退 models.d 的 params。
    #   NULL / {} = 未覆盖;{"max_model_len": 262144} = 只覆盖这一个键
    # 不需要 gpus 那种 `[]` 哨兵:params 是 dict,清某个键就是把它从 dict 里删掉,
    # 全清就是整列回 NULL —— 不存在「显式清空」与「未设置」语义冲突。
    # JSONB 而非 typed 多列:白名单会随 vLLM 版本增减,用 dict 才不用每次改 schema。
    params = Column(JSONB, nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_overrides(self) -> dict:
        """→ 与旧 overlay 同形状的 dict:{resident?, gpu?, vram_budget?, params?}(只含已设字段)。
        消费方(load_model_configs / registry / resolve_vram_utilization)契约不变,只换存储。"""
        out: dict = {}
        if self.resident is not None:
            out["resident"] = self.resident
        if self.gpu is not None:
            out["gpu"] = self.gpu
        if self.gpus is not None:
            # 空列表照样要落进 overrides —— 它是"显式清空组"的哨兵,不是"没设"。
            out["gpus"] = [int(i) for i in self.gpus]
        if self.vram_budget_mode is not None:
            vb: dict = {"mode": self.vram_budget_mode}
            if self.vram_budget_value is not None:
                vb["value"] = self.vram_budget_value
            out["vram_budget"] = vb
        if self.params:
            out["params"] = dict(self.params)
        return out
