#!/usr/bin/env bash
###############################################################################
# Quick sudo enabler for capstone validation
# Run this FIRST in your terminal:
#   bash ~/mininet/examples/enable_sudo_session.sh
# Then run the validation in the SAME terminal:
#   bash ~/mininet/examples/run_complete_validation.sh
###############################################################################

echo "This will cache your sudo credentials for the validation session."
echo "You will be prompted for your password once."
echo ""

# Cache sudo credentials
sudo -v

if sudo -n true 2>/dev/null; then
  echo ""
  echo "✅ Sudo access cached successfully!"
  echo ""
  echo "Now run the validation script in this same terminal:"
  echo "  source ~/sdn-env/bin/activate"
  echo "  cd ~/mininet"
  echo "  bash examples/run_complete_validation.sh"
  echo ""
  
  # Keep sudo alive in background for up to 30 minutes
  (while true; do sudo -n true 2>/dev/null; sleep 50; done) &
  KEEPALIVE_PID=$!
  echo "Sudo keepalive running (PID ${KEEPALIVE_PID}). It will expire when you close this terminal."
else
  echo "❌ Failed to cache sudo credentials."
  echo "Please try again or check your password."
  exit 1
fi
