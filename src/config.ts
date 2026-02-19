import dotenv from "dotenv";
import { z } from "zod";

dotenv.config();

const ConfigSchema = z.object({
  port: z.number().int().positive(),
  nodeEnv: z.string().min(1),
  discordWebhookUrl: z.string().min(1),
  maxUploadBytes: z.number().int().positive(),
  maxClockSkewSeconds: z.number().int().positive(),
  nonceTtlSeconds: z.number().int().positive(),
  rateLimitPerClientPerMinute: z.number().int().positive(),
  rateLimitRedeemPerIpPerMinute: z.number().int().positive(),
  logLevel: z.string().min(1),
  sqlitePath: z.string().min(1)
});

export type AppConfig = z.infer<typeof ConfigSchema>;

const DEFAULTS: Omit<AppConfig, "discordWebhookUrl"> = {
  port: 8080,
  nodeEnv: "production",
  maxUploadBytes: 26_214_400,
  maxClockSkewSeconds: 300,
  nonceTtlSeconds: 600,
  rateLimitPerClientPerMinute: 30,
  rateLimitRedeemPerIpPerMinute: 60,
  logLevel: "info",
  sqlitePath: "./data/relay.db"
};

function parseIntEnv(value: string | undefined): number | undefined {
  if (value === undefined || value === "") {
    return undefined;
  }

  const parsed = Number(value);
  if (!Number.isFinite(parsed) || !Number.isInteger(parsed)) {
    return undefined;
  }

  return parsed;
}

export function resolveConfig(
  overrides: Partial<AppConfig> = {},
  env: NodeJS.ProcessEnv = process.env
): AppConfig {
  const candidate = {
    port: parseIntEnv(env.PORT) ?? DEFAULTS.port,
    nodeEnv: env.NODE_ENV ?? DEFAULTS.nodeEnv,
    discordWebhookUrl: env.DISCORD_WEBHOOK_URL,
    maxUploadBytes:
      parseIntEnv(env.MAX_UPLOAD_BYTES) ?? DEFAULTS.maxUploadBytes,
    maxClockSkewSeconds:
      parseIntEnv(env.MAX_CLOCK_SKEW_SECONDS) ?? DEFAULTS.maxClockSkewSeconds,
    nonceTtlSeconds:
      parseIntEnv(env.NONCE_TTL_SECONDS) ?? DEFAULTS.nonceTtlSeconds,
    rateLimitPerClientPerMinute:
      parseIntEnv(env.RATE_LIMIT_PER_CLIENT_PER_MINUTE) ??
      DEFAULTS.rateLimitPerClientPerMinute,
    rateLimitRedeemPerIpPerMinute:
      parseIntEnv(env.RATE_LIMIT_REDEEM_PER_IP_PER_MINUTE) ??
      DEFAULTS.rateLimitRedeemPerIpPerMinute,
    logLevel: env.LOG_LEVEL ?? DEFAULTS.logLevel,
    sqlitePath: env.SQLITE_PATH ?? DEFAULTS.sqlitePath,
    ...overrides
  };

  return ConfigSchema.parse(candidate);
}

export function loadConfig(): AppConfig {
  return resolveConfig();
}
