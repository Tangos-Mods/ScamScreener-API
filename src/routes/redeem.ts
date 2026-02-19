import type Database from "better-sqlite3";
import type { FastifyInstance } from "fastify";
import { z } from "zod";

import type { AppConfig } from "../config";
import type { InviteCodeRecord } from "../db";
import { hashInviteCode, randomToken } from "../security/signature";
import type {
  ApiErrorResponse,
  RedeemSuccessResponse
} from "../types/api";

interface RedeemRouteDeps {
  db: Database.Database;
  config: AppConfig;
}

const RedeemBodySchema = z.object({
  inviteCode: z.string().min(1),
  installId: z.string().uuid(),
  modVersion: z.string().min(1)
});

function apiError(errorCode: ApiErrorResponse["errorCode"]): ApiErrorResponse {
  return { ok: false, errorCode };
}

export async function registerRedeemRoute(
  app: FastifyInstance,
  deps: RedeemRouteDeps
): Promise<void> {
  app.post(
    "/api/v1/client/redeem",
    {
      config: {
        rateLimit: {
          max: deps.config.rateLimitRedeemPerIpPerMinute,
          timeWindow: "1 minute"
        }
      }
    },
    async (request, reply) => {
      const parsedBody = RedeemBodySchema.safeParse(request.body);
      if (!parsedBody.success) {
        return reply.code(400).send(apiError("INVITE_INVALID"));
      }

      const now = new Date();
      const nowIso = now.toISOString();
      const codeHash = hashInviteCode(parsedBody.data.inviteCode);

      const invite = deps.db
        .prepare(
          `
            SELECT code_hash, max_uses, used_count, expires_at, created_at, created_by
            FROM invite_codes
            WHERE code_hash = ?
            LIMIT 1
          `
        )
        .get(codeHash) as InviteCodeRecord | undefined;

      if (!invite) {
        return reply.code(400).send(apiError("INVITE_INVALID"));
      }

      if (invite.expires_at && new Date(invite.expires_at).getTime() <= now.getTime()) {
        return reply.code(410).send(apiError("INVITE_EXPIRED"));
      }

      if (invite.used_count >= invite.max_uses) {
        return reply.code(409).send(apiError("INVITE_ALREADY_USED"));
      }

      const clientId = randomToken("relay-client-", 12);
      const clientSecret = randomToken("relay-secret-", 24);

      const tx = deps.db.transaction(() => {
        const inviteUpdate = deps.db
          .prepare(
            `
              UPDATE invite_codes
              SET used_count = used_count + 1
              WHERE code_hash = ?
                AND used_count < max_uses
            `
          )
          .run(codeHash);

        if (inviteUpdate.changes !== 1) {
          throw new Error("INVITE_ALREADY_USED");
        }

        deps.db
          .prepare(
            `
              INSERT INTO clients (client_id, client_secret, install_id, active, created_at, revoked_at)
              VALUES (?, ?, ?, 1, ?, NULL)
            `
          )
          .run(clientId, clientSecret, parsedBody.data.installId, nowIso);
      });

      try {
        tx();
      } catch (error) {
        if (error instanceof Error && error.message === "INVITE_ALREADY_USED") {
          return reply.code(409).send(apiError("INVITE_ALREADY_USED"));
        }

        throw error;
      }

      const response: RedeemSuccessResponse = {
        ok: true,
        clientId,
        clientSecret,
        signatureVersion: "v1"
      };

      return reply.code(200).send(response);
    }
  );
}
