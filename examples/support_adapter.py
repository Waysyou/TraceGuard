"""Explicit local plugin example; reusable answer/citation contract, no core edit."""
from traceguard.advisor_adapter import AdvisorAdapter


class SupportAdapter(AdvisorAdapter):
    name = "support"
    domain = "support"
    success_label = "客服答复正确率"
    outcome_label = "客服答复交付率"

    def normalize_case(self, case):
        normalized = super().normalize_case(case)
        normalized["agent_domain"] = self.domain
        return normalized


ADAPTER = SupportAdapter()
