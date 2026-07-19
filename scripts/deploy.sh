#!/usr/bin/env bash

# another_aws_health_telegram_bot - deployment script
#
# Packages the Lambda source, uploads it to S3 and deploys the
# CloudFormation stack. No external Python dependencies are vendored:
# the runtime only needs the standard library and boto3.
#
# Usage:
#   TELEGRAM_BOT_TOKEN=123:abc ./scripts/deploy.sh --bucket my-artifacts-bucket
#
# Options (env vars in parentheses override the defaults):
#   --bucket BUCKET     S3 bucket for the deployment package (ARTIFACTS_BUCKET) [required]
#   --stack-name NAME   CloudFormation stack name (STACK_NAME)
#   --region REGION     AWS region (AWS_REGION)
#   --bot-name NAME     BotName parameter (BOT_NAME)
#   --chat-id ID        TelegramChatId parameter (TELEGRAM_CHAT_ID)
#   --rules-file FILE   JSON file with the RoutingRules parameter (ROUTING_RULES_FILE)
#   --schedule EXPR     ScheduleExpression parameter (SCHEDULE_EXPRESSION)
#   --skip-tests        Do not run the test suite before deploying
#
# TELEGRAM_BOT_TOKEN is required on the first deploy (it seeds the
# Secrets Manager secret); on later deploys omit it to keep the stored one.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
fail()  { echo -e "${RED}[ERROR]${NC} $1" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TEMPLATE_FILE="$PROJECT_ROOT/infrastructure/template.yaml"
BUILD_DIR="$PROJECT_ROOT/build"
ZIP_FILE="$BUILD_DIR/lambda.zip"

STACK_NAME="${STACK_NAME:-another-aws-health-telegram-bot}"
AWS_REGION="${AWS_REGION:-eu-west-1}"
BOT_NAME="${BOT_NAME:-another-aws-health-telegram-bot}"
ARTIFACTS_BUCKET="${ARTIFACTS_BUCKET:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
ROUTING_RULES_FILE="${ROUTING_RULES_FILE:-}"
SCHEDULE_EXPRESSION="${SCHEDULE_EXPRESSION:-}"
SKIP_TESTS="${SKIP_TESTS:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket)      ARTIFACTS_BUCKET="$2"; shift 2 ;;
    --stack-name)  STACK_NAME="$2"; shift 2 ;;
    --region)      AWS_REGION="$2"; shift 2 ;;
    --bot-name)    BOT_NAME="$2"; shift 2 ;;
    --chat-id)     TELEGRAM_CHAT_ID="$2"; shift 2 ;;
    --rules-file)  ROUTING_RULES_FILE="$2"; shift 2 ;;
    --schedule)    SCHEDULE_EXPRESSION="$2"; shift 2 ;;
    --skip-tests)  SKIP_TESTS=1; shift ;;
    -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)             fail "unknown option: $1" ;;
  esac
done

command -v aws >/dev/null || fail "aws CLI not found"
[[ -n "$ARTIFACTS_BUCKET" ]] || fail "--bucket (or ARTIFACTS_BUCKET) is required"

if [[ -z "$SKIP_TESTS" ]]; then
  if command -v pytest >/dev/null; then
    info "Running test suite..."
    (cd "$PROJECT_ROOT" && python -m pytest tests/ -q) || fail "tests failed"
    ok "Tests passed"
  else
    info "pytest not found, skipping tests (install dev deps or use --skip-tests to silence)"
  fi
fi

info "Validating CloudFormation template..."
aws cloudformation validate-template \
  --region "$AWS_REGION" \
  --template-body "file://$TEMPLATE_FILE" >/dev/null
ok "Template is valid"

info "Packaging Lambda source..."
mkdir -p "$BUILD_DIR"
rm -f "$ZIP_FILE"
(cd "$PROJECT_ROOT" && zip -qr "$ZIP_FILE" src -x 'src/__pycache__/*')
ok "Package created: $ZIP_FILE"

CODE_KEY="$BOT_NAME/lambda-$(date +%Y%m%d%H%M%S).zip"
info "Uploading package to s3://$ARTIFACTS_BUCKET/$CODE_KEY ..."
aws s3 cp "$ZIP_FILE" "s3://$ARTIFACTS_BUCKET/$CODE_KEY" --region "$AWS_REGION" >/dev/null
ok "Package uploaded"

PARAMS=(
  "BotName=$BOT_NAME"
  "CodeS3Bucket=$ARTIFACTS_BUCKET"
  "CodeS3Key=$CODE_KEY"
)
[[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] && PARAMS+=("TelegramBotToken=$TELEGRAM_BOT_TOKEN")
[[ -n "$TELEGRAM_CHAT_ID" ]] && PARAMS+=("TelegramChatId=$TELEGRAM_CHAT_ID")
[[ -n "$SCHEDULE_EXPRESSION" ]] && PARAMS+=("ScheduleExpression=$SCHEDULE_EXPRESSION")
if [[ -n "$ROUTING_RULES_FILE" ]]; then
  [[ -f "$ROUTING_RULES_FILE" ]] || fail "rules file not found: $ROUTING_RULES_FILE"
  PARAMS+=("RoutingRules=$(tr -d '\n' < "$ROUTING_RULES_FILE")")
fi

info "Deploying stack $STACK_NAME in $AWS_REGION ..."
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE_FILE" \
  --parameter-overrides "${PARAMS[@]}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset
ok "Deployment completed"

aws cloudformation describe-stacks \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs' \
  --output table
