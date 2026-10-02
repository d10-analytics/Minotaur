CREATE TABLE dbo.Parent (id int PRIMARY KEY)
GO
CREATE TABLE dbo.Child (id int, parent_id int REFERENCES dbo.Parent(id))
GO
CREATE FUNCTION dbo.IdentityValue(@value int) RETURNS int AS RETURN @value
GO
CREATE VIEW dbo.ChildValues AS SELECT dbo.IdentityValue(id) AS value FROM dbo.Child
GO
