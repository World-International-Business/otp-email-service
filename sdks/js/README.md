# MailHub SDK (Node.js)

Client officiel — OTP par email, emails transactionnels, statuts d'envoi.
Node ≥ 18 (fetch + crypto natifs, zéro dépendance).

```js
const { MailHubClient } = require("mailhub-sdk");

const client = new MailHubClient({
  baseUrl: "https://mail.maboite.com",
  apiKey: "mk_...",
  hmacSecret: "whsec_...",
});

// OTP
const res = await client.sendOtp({
  userId: "usr_123", email: "user@ex.com",
  countryCode: "FR", purpose: "login_2fa", language: "fr",
});
const ok = await client.verifyOtp(res.request_id, "123456");

// Email
const msg = await client.sendEmail({
  toEmail: "client@ex.com", type: "notification",
  data: { title: "Commande", message: "Expédiée" },
  idempotencyKey: "cmd-8842",
  // sendAt: "2026-12-01T09:00:00Z"  — envoi programmé
});
console.log(await client.emailStatus(msg.message_id));
```

Erreurs : `MailHubError` avec `.status` et `.detail`.
