# Sync mirror table (SYNC_TABLE, docs/decisions.md S10). Exactly what app/sync/dynamodb.py ensure_table would
# create: on-demand, pk (HASH, S) + sk (RANGE, S), nothing else. The runtime role can only DescribeTable and
# PutItem, so the app finds it ACTIVE and never tries CreateTable.
resource "aws_dynamodb_table" "sync" {
  name         = "downtime-copilot-sync-${random_id.suffix.hex}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }
}
