import Fastify from "fastify";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
  DynamoDBDocumentClient,
  GetCommand,
  ScanCommand,
  type ScanCommandInput,
} from "@aws-sdk/lib-dynamodb";

const app = Fastify({ logger: true });

const db = DynamoDBDocumentClient.from(
  new DynamoDBClient({
    region: process.env.AWS_REGION ?? "us-west-2",
  }),
);

const execFileAsync = promisify(execFile);

const projectRoot = fileURLToPath(new URL("../../../", import.meta.url));
const pythonPath = `${projectRoot}venv/bin/python`;

app.get("/api/health", async () => {
  return { status: "ok" };
});

app.get("/api/risks", async (_request, reply) => {
  try {
    const risks: Record<string, unknown>[] = [];
    let cursor: ScanCommandInput["ExclusiveStartKey"];

    do {
      const page = await db.send(
        new ScanCommand({
          TableName: "supplier_risk_state",
          ExclusiveStartKey: cursor,
        }),
      );

      risks.push(...(page.Items ?? []));
      cursor = page.LastEvaluatedKey;
    } while (cursor);

    risks.sort((a, b) =>
      String(b.last_seen_at ?? "").localeCompare(
        String(a.last_seen_at ?? ""),
      ),
    );

    return { risks, count: risks.length };
  } catch (error) {
    app.log.error(error, "Failed to load risks");

    return reply.code(503).send({
      error: "Risk data is currently unavailable",
    });
  }
});

app.get("/api/actions", async (_request, reply) => {
  try {
    const actions: Record<string, unknown>[] = [];
    let cursor: ScanCommandInput["ExclusiveStartKey"];

    do {
      const page = await db.send(
        new ScanCommand({
          TableName: "risk_agent_actions",
          ExclusiveStartKey: cursor,
        }),
      );

      actions.push(...(page.Items ?? []));
      cursor = page.LastEvaluatedKey;
    } while (cursor);

    actions.sort((a, b) =>
      String(b.created_at ?? "").localeCompare(
        String(a.created_at ?? ""),
      ),
    );

    return { actions, count: actions.length };
  } catch (error) {
    app.log.error(error, "Failed to load actions");

    return reply.code(503).send({
      error: "Action data is currently unavailable",
    });
  }
});

app.get("/api/authority", async (_request, reply) => {
  try {
    const result = await db.send(
      new GetCommand({
        TableName: "risk_agent_authority",
        Key: { scope_id: "agent#supplier-risk-agent" },
        ConsistentRead: true,
      }),
    );

    if (!result.Item) {
      return reply.code(404).send({
        error: "Agent authority record not found",
      });
    }

    return { authority: result.Item };
  } catch (error) {
    app.log.error(error, "Failed to load authority");

    return reply.code(503).send({
      error: "Authority data is currently unavailable",
    });
  }
});

app.post<{ Body: { reason: string } }>(
  "/api/authority/revoke",
  {
    schema: {
      body: {
        type: "object",
        required: ["reason"],
        additionalProperties: false,
        properties: {
          reason: {
            type: "string",
            minLength: 1,
            maxLength: 500,
          },
        },
      },
    },
  },
  async (request, reply) => {
    const reason = request.body.reason.trim();

    if (!reason) {
      return reply.code(400).send({
        error: "A revocation reason is required",
      });
    }

    try {
      await execFileAsync(
        pythonPath,
        [
          "-m",
          "app.governance.authority_cli",
          "revoke",
          "--reason",
          reason,
          "--actor",
          "operator-ken",
        ],
        {
          cwd: projectRoot,
          env: {
            ...process.env,
            AUTHORITY_TABLE: "risk_agent_authority",
            AWS_DEFAULT_REGION: process.env.AWS_REGION ?? "us-west-2",
          },
          timeout: 30_000,
          maxBuffer: 1024 * 1024,
        },
      );

      return { message: "Revocation command completed" };
    } catch (error) {
      app.log.error(error, "Revocation command failed");

      return reply.code(503).send({
        error:
          "Could not confirm revocation. Refresh authority before retrying.",
      });
    }
  },
);

app.post<{ Body: { reason: string } }>(
  "/api/authority/restore",
  {
    schema: {
      body: {
        type: "object",
        required: ["reason"],
        additionalProperties: false,
        properties: {
          reason: {
            type: "string",
            minLength: 1,
            maxLength: 500,
          },
        },
      },
    },
  },
  async (request, reply) => {
    const reason = request.body.reason.trim();

    if (!reason) {
      return reply.code(400).send({
        error: "A restoration reason is required",
      });
    }

    try {
      await execFileAsync(
        pythonPath,
        [
          "-m",
          "app.governance.authority_cli",
          "restore",
          "--reason",
          reason,
          "--actor",
          "operator-ken",
        ],
        {
          cwd: projectRoot,
          env: {
            ...process.env,
            AUTHORITY_TABLE: "risk_agent_authority",
            AWS_DEFAULT_REGION: process.env.AWS_REGION ?? "us-west-2",
          },
          timeout: 30_000,
          maxBuffer: 1024 * 1024,
        },
      );

      return { message: "Restoration command completed" };
    } catch (error) {
      app.log.error(error, "Restoration command failed");

      return reply.code(503).send({
        error:
          "Could not confirm restoration. Refresh authority before retrying.",
      });
    }
  },
);

await app.listen({
  port: 3001,
  host: "127.0.0.1",
});