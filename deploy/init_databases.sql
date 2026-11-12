-- Create isolated databases for microservices
CREATE DATABASE main_app_db;
CREATE DATABASE auth_service_db;

-- Grant privileges
GRANT ALL PRIVILEGES ON DATABASE main_app_db TO postgres;
GRANT ALL PRIVILEGES ON DATABASE auth_service_db TO postgres;
