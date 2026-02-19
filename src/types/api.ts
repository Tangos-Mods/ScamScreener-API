export type RedeemErrorCode =
  | "INVITE_INVALID"
  | "INVITE_EXPIRED"
  | "INVITE_ALREADY_USED"
  | "RATE_LIMITED";

export type UploadErrorCode =
  | "AUTH_FAILED"
  | "NONCE_REPLAY"
  | "PAYLOAD_INVALID"
  | "FILE_TOO_LARGE"
  | "RATE_LIMITED"
  | "DISCORD_FORWARD_FAILED";

export type ApiErrorCode = RedeemErrorCode | UploadErrorCode;

export interface ApiErrorResponse {
  ok: false;
  errorCode: ApiErrorCode;
  requestId?: string;
}

export interface RedeemRequestBody {
  inviteCode: string;
  installId: string;
  modVersion: string;
}

export interface RedeemSuccessResponse {
  ok: true;
  clientId: string;
  clientSecret: string;
  signatureVersion: "v1";
}

export interface UploadMetadata {
  schemaVersion: "1";
  modVersion: string;
  aiModelVersion: string;
  playerName: string;
  playerUuid: string;
  clientTimestamp: string;
  fileSha256: string;
  fileSizeBytes: number;
}

export interface UploadSuccessResponse {
  ok: true;
  requestId: string;
  discordMessageId: string;
  verifiedFileSha256: string;
}
