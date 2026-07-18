-- Database initialization script for PostgreSQL
-- This script is run by docker-compose during initial database setup

-- Create extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- For text search

-- Create custom types
DO $$ BEGIN
    CREATE TYPE user_role AS ENUM ('user', 'admin', 'developer');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

-- 对外模型网关（LiteLLM Proxy）专用独立逻辑库，与主库 hugagent 隔离（避免 alembic 互相污染）。
-- 仅首次初始化（空数据卷）时建；存量部署需手动 `CREATE DATABASE litellm OWNER hugagent_user;`。
-- LiteLLM 镜像启动时用 prisma 在该库内建自己的表。
SELECT 'CREATE DATABASE litellm OWNER hugagent_user'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'litellm')\gexec

-- Grant necessary permissions
GRANT ALL PRIVILEGES ON DATABASE hugagent TO hugagent_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO hugagent_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO hugagent_user;

-- Create indexes for common queries (will be created by Alembic, but good to have here for reference)
-- These are examples and should match your actual schema

-- Enable row-level security (optional, for future use)
-- ALTER TABLE users_shadow ENABLE ROW LEVEL SECURITY;

-- Create audit trigger function (optional enhancement)
CREATE OR REPLACE FUNCTION audit_trigger_func()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Success message
DO $$
BEGIN
    RAISE NOTICE 'Database initialization completed successfully';
END $$;
