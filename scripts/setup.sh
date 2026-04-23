#!/bin/bash
set -e

echo "📦 Installing npm dependencies..."
npm install

echo "🐍 Setting up Lambda layer..."
mkdir -p lambda-layer/python
pip3 install boto3 botocore -t lambda-layer/python/ --upgrade --quiet

echo "🔨 Building TypeScript..."
npx tsc

echo "✅ Setup complete! Run 'cdk deploy' to deploy."
