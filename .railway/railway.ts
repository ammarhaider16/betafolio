import { defineRailway, github, postgres, project, service } from "railway/iac";

// Set BETAFOLIO_REPOSITORY=owner/repository in the operator's shell.
// Define the referenced shared variables in the Railway environment, never in Git.
export default defineRailway((ctx) => {
  const repository = process.env.BETAFOLIO_REPOSITORY;
  if (!repository) throw new Error("Set BETAFOLIO_REPOSITORY to your GitHub owner/repository");
  const db = postgres("postgres");
  const source = github(repository);
  const env = {
    DATABASE_URL: db.env.DATABASE_URL,
    BETAFOLIO_ENCRYPTION_KEY: ctx.shared.BETAFOLIO_ENCRYPTION_KEY,
    PORTFOLIO_PUBLIC_URL: ctx.shared.PORTFOLIO_PUBLIC_URL,
    PORTFOLIO_SERVER_MODE: "1",
    HOME: "/tmp",
    XDG_CACHE_HOME: "/tmp/cache",
  };
  const web = service("web", {
    source,
    start: "/app/.venv/bin/betafolio serve --server --host 0.0.0.0",
    healthcheck: "/health",
    healthcheckTimeout: 120,
    replicas: 1,
    env: {
      ...env,
      BETAFOLIO_BOOTSTRAP_ADMIN_EMAIL: ctx.shared.BETAFOLIO_BOOTSTRAP_ADMIN_EMAIL,
      BETAFOLIO_BOOTSTRAP_ADMIN_PASSWORD: ctx.shared.BETAFOLIO_BOOTSTRAP_ADMIN_PASSWORD,
    },
  });
  const worker = service("worker", {
    source,
    start: "/app/.venv/bin/betafolio worker",
    replicas: 1,
    env,
  });
  return project("betafolio", { resources: [db, web, worker] });
});
