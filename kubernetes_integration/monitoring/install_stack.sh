#!/bin/bash
# install_stack.sh
# Sets up the baseline observability stack and Autofixops namespace

set -e

echo "🚀 Starting Phase 1 Stack Installation..."

# 1. Create Namespace
echo "📦 Creating autofixops namespace..."
kubectl create namespace autofixops --dry-run=client -o yaml | kubectl apply -f -

# 2. Add Helm Repos
echo "🔄 Adding Helm Repositories..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

# 3. Install kube-prometheus-stack
echo "📊 Installing kube-prometheus-stack (Metrics & Alerts)..."
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace autofixops \
  --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false

# 4. Install Loki Stack
echo "📜 Installing Loki-stack (Logs)..."
helm upgrade --install loki grafana/loki-stack \
  --namespace autofixops \
  --set grafana.enabled=false # grafana is already installed with prometheus

# 5. Apply explicit alerting rules
echo "🚨 Applying custom alerting rules..."
kubectl apply -f alerts.yaml -n autofixops

echo "✅ Success! The failure playground is ready."
echo "➡️  Next steps: Deploy the target app and run chaos scripts."
