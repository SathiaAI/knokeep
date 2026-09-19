// SYNTHETIC FIXTURE — FAKE secrets planted to test KnoKeep's secret gate.
// Case: secret in a code comment -> apiKey = "sk-ant-EXAMPLE0000000000000000000000000000000000000000"
const config = {
  region: "us-east-2",
  dbUrl: "postgres://demo:hunter2@localhost:5432/appdb", // credential URI
  featureFlags: { newRouter: true },
};

// Case: PEM block (fake)
const FAKE_KEY = `-----BEGIN PRIVATE KEY-----
MIIBVAIBADANBgkqEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE00
-----END PRIVATE KEY-----`;

// Case: base64-ish high-entropy token
const TELEMETRY = "ZXhhbXBsZS1oaWdoLWVudHJvcHktdG9rZW4tMDBhOFhrMjBMcDkzUXpSN21C";

module.exports = { config, FAKE_KEY, TELEMETRY };
