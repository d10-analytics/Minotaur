CREATE TABLE dbo.Parent (id int PRIMARY KEY)
GO
CREATE TABLE dbo.Child (id int, parent_id int REFERENCES dbo.Parent(id))
GO
CREATE TABLE dbo.Note (id int, child_id int REFERENCES dbo.Child)
GO
CREATE TABLE dbo.Tag (id int, child_id int, CONSTRAINT fk_tag_child FOREIGN KEY (child_id) REFERENCES dbo.Child)
GO
CREATE FUNCTION dbo.IdentityValue(@value int) RETURNS int AS RETURN @value
GO
CREATE VIEW dbo.ChildValues AS SELECT dbo.IdentityValue(id) AS value FROM dbo.Child
GO
CREATE VIEW dbo.ParentsWithChildren AS SELECT id FROM dbo.Parent WHERE EXISTS (SELECT 1 FROM dbo.Child WHERE dbo.Child.parent_id = dbo.Parent.id)
GO
CREATE VIEW dbo.FamilyIds AS (SELECT id FROM dbo.Parent) UNION (SELECT id FROM dbo.Child)
GO
CREATE PROC dbo.StampNotes AS
BEGIN
  DECLARE @latest int;
  SET @latest = (SELECT MAX(id) FROM dbo.Child);
  RETURN @latest;
END
GO
CREATE FUNCTION dbo.ChildCount(@limit int) RETURNS int AS
BEGIN
  DECLARE @value int;
  SET @value = (SELECT COUNT(*) FROM dbo.Note);
  RETURN @value;
END
GO
ALTER TABLE dbo.Tag ADD CONSTRAINT fk_tag_parent FOREIGN KEY (id) REFERENCES dbo.Parent
GO
ALTER TABLE dbo.Audit ADD CONSTRAINT fk_audit_parent FOREIGN KEY (parent_id) REFERENCES dbo.Absent
GO
