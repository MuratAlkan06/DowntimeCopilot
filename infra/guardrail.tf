# Published guardrail for policy answers (GUARDRAIL_ID / GUARDRAIL_VERSION, docs/decisions.md S3). The
# thresholds are the workshop guardrail's (grounding 0.75, relevance 0.5); the topic wording and messages are
# a reconstruction, since that account is gone (docs/decisions.md, "Guardrail reconstruction").
locals {
  guardrail_blocked_message = "The safety check blocked this answer. I can't give dosing or treatment advice: follow the provider's order and the patient's medication record."
}

resource "aws_bedrock_guardrail" "main" {
  name                      = "downtime-copilot-v1"
  description               = "Downtime Copilot: denies dosing or treatment advice not in the record or an order; checks grounding of policy answers."
  blocked_input_messaging   = local.guardrail_blocked_message
  blocked_outputs_messaging = local.guardrail_blocked_message

  topic_policy_config {
    topics_config {
      name       = "dosing-treatment-advice"
      type       = "DENY"
      definition = "Dosing or treatment advice that is not stated in the patient record or an existing provider order."
      examples = [
        "How much insulin should I give?",
        "What dose of metoprolol is right here?",
        "Can I double the heparin drip?",
      ]
    }
  }

  contextual_grounding_policy_config {
    filters_config {
      type      = "GROUNDING"
      threshold = 0.75
    }

    filters_config {
      type      = "RELEVANCE"
      threshold = 0.5
    }
  }
}

# The app calls ApplyGuardrail on a published version, never DRAFT. skip_destroy keeps a published version
# when this resource is replaced or destroyed.
resource "aws_bedrock_guardrail_version" "main" {
  guardrail_arn = aws_bedrock_guardrail.main.guardrail_arn
  description   = "Published version for the app (GUARDRAIL_VERSION)."
  skip_destroy  = true
}
