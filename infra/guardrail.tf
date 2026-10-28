# Published guardrail for policy answers (GUARDRAIL_ID / GUARDRAIL_VERSION, docs/decisions.md S3). The
# thresholds are calibrated: a replay of the refusal eval's 35 checks against version 1
# (tools/guardrail_calibrate.py) gave grounding 0.75 under the frozen #80 formula, and relevance stays 0.5
# (docs/decisions.md, 2026-10-28, #80 S3). The denied topic sees only the answer text, so its examples are
# answer-shaped. The messages are a reconstruction (docs/decisions.md, "Guardrail reconstruction").
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
      definition = "Recommending, selecting, or directing a medication, dose, rate, route, or treatment change that is not stated in the patient record or an existing provider order."
      examples = [
        "Give 6 units of regular insulin now and recheck the glucose in an hour.",
        "Start a heparin drip at 12 units/kg/hr for the new clot.",
        "You can safely give 2 mg of IV morphine for that pain level.",
        "Hold tonight's anticoagulant dose until the INR comes down.",
        "Switch him from the IV antibiotic to the oral form now.",
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
