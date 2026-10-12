-- A second database for tests/test_migrations.py, which applies throwaway migrations and needs a schema it can empty freely without touching the one the app tests run against.
CREATE DATABASE instant_news_scratch CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
GRANT ALL PRIVILEGES ON instant_news_scratch.* TO 'instant_news_test'@'%';
