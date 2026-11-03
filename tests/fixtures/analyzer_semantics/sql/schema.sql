CREATE TABLE dbo.Parent (id int PRIMARY KEY)
GO
CREATE TABLE dbo.Child (id int, parent_id int REFERENCES dbo.Parent(id))
GO
CREATE FUNCTION dbo.IdentityValue(@value int) RETURNS int AS RETURN @value
GO
CREATE VIEW dbo.ChildValues AS SELECT dbo.IdentityValue(id) AS value FROM dbo.Child
GO
CREATE VIEW dbo.ParentsWithChildren AS SELECT id FROM dbo.Parent WHERE EXISTS (SELECT 1 FROM dbo.Child WHERE dbo.Child.parent_id = dbo.Parent.id)
GO
CREATE VIEW dbo.FamilyIds AS (SELECT id FROM dbo.Parent) UNION (SELECT id FROM dbo.Child)
GO
