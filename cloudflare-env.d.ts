declare namespace Cloudflare {
  interface Env {
    DB?: D1Database;
    COLLECTOR_SITE_TOKEN?: string;
    BUCKET?: R2Bucket;
  }
}
