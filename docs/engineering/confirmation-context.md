# Shared confirmation context

`confirmation_display.context_window` is the common Local/Feishu DTO. It keeps the complete marked source span and callback values unchanged. Immediate Chinese/mixed context uses 48 conservative display clusters per side; English uses 24 complete words including internal apostrophes. Unused beginning/end allowance transfers to the other side.

The stdlib boundary routine keeps combining marks, modifiers, ZWJ sequences, paired regional indicators and virama sequences together. It is conservative, not a claim of full Unicode grapheme conformance. Source coordinates remain code points; stale or cluster-splitting marked spans fail explicitly rather than guessing offsets. The algorithm identifier accompanies each DTO.

Local expanded cards expose the shared window. The dedicated confirmation-context route renders the same window. Feishu uses that full marked range; overlong marked text is summarized at cluster boundaries and its existing full-text pagination remains available. Platform capacity and actual desktop/mobile presentation are separate acceptance evidence; unit/Flask tests do not establish them.

Validation: seven display tests and adjacent Feishu/Web tests pass on CPython 3.11.16 (44 total). The former twelve-character expectation was replaced by the frozen shared-window contract. Actual viewport/client checks remain pending.
