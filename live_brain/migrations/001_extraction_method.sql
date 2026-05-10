-- Migration 001: Add extraction_method column to track automatic vs manual extraction
-- This enables Feature 1: Automatic Memory Extraction

-- Add extraction_method to facts table
ALTER TABLE facts ADD COLUMN extraction_method TEXT DEFAULT 'manual';

-- Add extraction_method to beliefs table
ALTER TABLE beliefs ADD COLUMN extraction_method TEXT DEFAULT 'manual';

-- Add extraction_method to entities table
ALTER TABLE entities ADD COLUMN extraction_method TEXT DEFAULT 'manual';

-- Create index for filtering by extraction method
CREATE INDEX IF NOT EXISTS idx_facts_extraction ON facts(extraction_method);
CREATE INDEX IF NOT EXISTS idx_beliefs_extraction ON beliefs(extraction_method);
