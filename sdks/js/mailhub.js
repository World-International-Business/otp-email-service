/**
 * MailHub — client Node.js officiel (Node ≥ 18, fetch + crypto natifs).
 *
 *   const { MailHubClient } = require("./mailhub");
 *   const client = new MailHubClient({
 *     baseUrl: "https://mail.maboite.com",
 *     apiKey: "mk_...",
 *     hmacSecret: "whsec_...",
 *   });
 *   const res = await client.sendOtp({
 *     userId: "usr_123", email: "user@ex.com",
 *     countryCode: "FR", purpose: "login_2fa", language: "fr",
 *   });
 */

const crypto = require("crypto");

class MailHubError extends Error {
  constructor(status, detail) {
    super(`MailHub API error ${status}: ${JSON.stringify(detail)}`);
    this.status = status;
    this.detail = detail;
  }
}

class MailHubClient {
  constructor({ baseUrl, apiKey, hmacSecret, timeoutMs = 15000 }) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey;
    this.hmacSecret = hmacSecret;
    this.timeoutMs = timeoutMs;
  }

  _sign(body) {
    const ts = String(Math.floor(Date.now() / 1000));
    const nonce = crypto.randomBytes(16).toString("hex");
    const sig = crypto
      .createHmac("sha256", this.hmacSecret)
      .update(body + ts + "|" + nonce)
      .digest("hex");
    return {
      "X-API-Key": this.apiKey,
      "X-Timestamp": ts,
      "X-Nonce": nonce,
      "X-HMAC-Signature": sig,
      "Content-Type": "application/json",
    };
  }

  async _request(method, path, payload, headers = {}) {
    // Le serveur signe le corps brut : GET = corps vide.
    const body = payload ? JSON.stringify(payload) : "";
    const res = await fetch(this.baseUrl + path, {
      method,
      headers: { ...this._sign(body), ...headers },
      body: method === "GET" ? undefined : body,
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw new MailHubError(res.status, json.detail ?? res.statusText);
    return json;
  }

  health() {
    return fetch(this.baseUrl + "/health").then((r) => r.json());
  }

  sendOtp({ userId, email, countryCode, purpose, language = "en",
            magicLink = false, idempotencyKey }) {
    const payload = {
      user_id: userId, email, country_code: countryCode,
      purpose, language,
    };
    if (magicLink) payload.magic_link = true;
    return this._request("POST", "/v1/otp/send", payload,
      idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {});
  }

  verifyOtp(requestId, code) {
    return this._request("POST", "/v1/otp/verify",
      { request_id: requestId, code });
  }

  sendEmail({ toEmail, type, data, language = "en", subject, textBody,
              htmlBody, sendAt, idempotencyKey }) {
    const payload = { to_email: toEmail, type, language };
    if (data) payload.data = data;
    if (subject) payload.subject = subject;
    if (textBody) payload.text_body = textBody;
    if (htmlBody) payload.html_body = htmlBody;
    if (sendAt) payload.send_at = sendAt; // ISO 8601, ≤ 30 j
    return this._request("POST", "/v1/email/send", payload,
      idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {});
  }

  emailStatus(messageId) {
    return this._request("GET", `/v1/email/status/${messageId}`);
  }
}

module.exports = { MailHubClient, MailHubError };
