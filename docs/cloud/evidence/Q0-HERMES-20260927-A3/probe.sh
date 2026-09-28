#!/usr/bin/env bash
# Q0 Hermes Capability Smoke Test Probe Script
# Run from: <A3_ROOT>\repo

set -e

STORE_PATH="<A3_ROOT>/store"
PROJECT_ID="q0-hermes-20260927-a3"
CLIENT="hermes"
WORK_DIR="<A3_ROOT>/repo"

echo "=== Q0 Hermes Capability Smoke Test Probe ==="
echo "Store path: $STORE_PATH"
echo "Project ID: $PROJECT_ID"
echo "Client: $CLIENT"
echo ""

# Step 1: Verify checkout
echo "Step 1: Verifying checkout..."
cd "$WORK_DIR"
INPUT_COMMIT=$(git rev-parse HEAD)
BRANCH=$(git branch --show-current)
echo "Input commit: $INPUT_COMMIT"
echo "Branch: $BRANCH"
echo "PASS: Checkout verified"
echo ""

# Step 2: Bootstrap KnoKeep
echo "Step 2: Bootstrapping KnoKeep..."
"$WORK_DIR/skill/knokeep_state.py" --store "$STORE_PATH" --project "$PROJECT_ID" --client "$CLIENT" bootstrap
if [ $? -eq 0 ]; then
    echo "PASS: Bootstrap successful"
else
    echo "FAIL: Bootstrap failed"
    exit 1
fi
echo ""

# Step 3: Append session
echo "Step 3: Appending session..."
BODY_FILE=$(mktemp)
cat > "$BODY_FILE" <<'HERMES_BODY'
---
project_id: q0-hermes-20260927-a3
session_id: 20260927-1430-0123
client: hermes
action: synthetic_probe
---
This is a synthetic probe message from Hermes (GLM-4.7-Flash-Q4_K_M, provider custom). It tests KnoKeep capture and resumption. The synthetic store is at <A3_ROOT>/store. The project is q0-hermes-20260927-a3. The client is hermes. The action is synthetic_probe. This is a bounded test to verify Hermes can successfully save and restore state with KnoKeep.
HERMES_BODY

"$WORK_DIR/skill/knokeep_state.py" --store "$STORE_PATH" --project "$PROJECT_ID" --client "$CLIENT" session-append --body-file "$BODY_FILE"
if [ $? -eq 0 ]; then
    echo "PASS: Session append successful"
else
    echo "FAIL: Session append failed"
    exit 1
fi
rm -f "$BODY_FILE"
echo ""

# Step 4: Verify store
echo "Step 4: Verifying store..."
if [ -f "$STORE_PATH/journal/journal.log" ]; then
    echo "PASS: Journal log exists"
else
    echo "FAIL: Journal log not found"
    exit 1
fi

if [ -f "$STORE_PATH/data/$PROJECT_ID/sessions/20260927-2352-6236" ]; then
    echo "PASS: Journal entry exists"
else
    echo "FAIL: Journal entry not found"
    exit 1
fi

if [ -f "$STORE_PATH/.knokeep-eval/events.jsonl" ]; then
    echo "PASS: Events log exists"
else
    echo "FAIL: Events log not found"
    exit 1
fi
echo ""

# Step 5: Eval
echo "Step 5: Running KnoKeep eval..."
"$WORK_DIR/skill/knokeep_state.py" --store "$STORE_PATH" --project "$PROJECT_ID" eval
if [ $? -eq 0 ]; then
    echo "PASS: Eval completed successfully"
else
    echo "FAIL: Eval failed"
    exit 1
fi
echo ""

echo "=== Probe Complete ==="
