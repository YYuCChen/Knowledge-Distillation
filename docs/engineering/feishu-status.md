# Feishu business status and group actions

The receipt header projects six business states: 已接收／等待中、处理中、待你操作、已完成、未形成知识、需处理. Actionable judgment takes precedence over a blocking error, which precedes working, queued and terminal results. Task counts are exclusive: success, content rejection, technical failure, waiting confirmation and unfinished. Groups/positions and uncreated failed intake parts are reported separately. Action-queue toast text does not replace the business header.

Group buttons carry explicit scope, current group revision and request identity. The shared service performs atomic validation and replay; switching displayed member/context pages does not submit a judgment. Candidate/keep/manual apply to the displayed full scope, unable selects only the displayed member. Local cards allow arbitrary member subsets. Existing receipt ownership, patch-in-place and old single-member callbacks are retained.

The [official update-card API](https://open.feishu.cn/document/server-docs/im-v1/message-card/patch), read on 2026-09-14 (page last updated 2025-07-15), specifies a maximum card size of 30 KB and a per-message update limit of 5 QPS. Projection uses a conservative 28,000-byte target and 30,000-byte ceiling for serialized UTF-8 card JSON. It drops optional group candidate controls before the explicit scope or context navigation. Overlong context is available through paragraph controls. This local budget is not a successful live API send.

Synthetic coverage includes a 32-member card with explicit callback selections, long-candidate capacity, mixed outcomes and old actions. Actual API acceptance, desktop/mobile presentation, themes and phone reachability require separate authorized device evidence. No test result here claims those checks ran.
