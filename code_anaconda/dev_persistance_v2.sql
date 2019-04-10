-- CREATE SCHEMA scratch;
-- CREATE TABLE  scratch.work_div AS SELECT * from af_modvrs_na_2012.work_div;
-- CREATE TABLE  scratch.work_lrg AS SELECT * FROM af_modvrs_na_2012.work_lrg;

-- pure sql based answer is probably inefficient, better use external network library
-- https://www.fusionbox.com/blog/detail/graph-algorithms-in-a-database-recursive-ctes-and-topological-sort-with-postgres/620/
-- igraph is probably more efficient than networkx as it is implemented in C (networkx is pure python)
-- https://graph-tool.skewed.de/performance
-- But still, the approach I took was to load all the edges as array_agg (python list), which needs to be fixed (memory short)
-- maybe pass table to python function, then inside python, load the edges to graph and return connected components?
-- try divide and concur
-- 


SET search_path TO scratch, public;



-- assume that there is work_lrg
-- grab 10 deg by 10 deg subregion at a time
--   make near table
--   find overlaps (connected components)
--   pick the fire of overlapping detections
-- go through one more time grabbing everything?
-- you got list of fireid that overlaps
-- 
-- count # of days for each overlaps
-- if more than 10 day, or something like that, drop them

-- better make above to some kind of pgsql function or procedure, as i need to 
-- do this across different part of domains


-- determine extenty of domain, come up with list of tiles

DROP TABLE IF EXISTS tbl_ext0;
CREATE TABLE tbl_ext0 AS
WITH foo AS ( 
  --SELECT min(floor(ST_Xmin(geom_lrg))) xmn, min(ceil(ST_Ymin(geom_lrg))) ymn, max(floor(ST_XMax(geom_lrg))) xmx, max(ceil(ST_YMax(geom_lrg))) ymx, 10 dx, 10 dy
  SELECT min(floor(ST_Xmin(geom_lrg))) xmn, min(ceil(ST_Ymin(geom_lrg))) ymn, max(floor(ST_XMax(geom_lrg))) xmx, max(ceil(ST_YMax(geom_lrg))) ymx, 2 dx, 2 dy
  FROM work_lrg
)
SELECT xmn, xmx, ymn, ymx, dx, dy, ceil((xmx-xmn)/dx) nx, ceil((ymx-ymn)/dy) ny
FROM foo;


DROP TABLE IF EXISTS tbl_ext;
CREATE TABLE tbl_ext AS
WITH baz AS
(
  WITH foo AS 
  (
    SELECT row_number()  over ( ) - 1 idx
    FROM (
      SELECT unnest(array_fill(1, array[nx::integer]))
      FROM tbl_ext0
    ) x
  ), 
  bar AS 
  (
    SELECT row_number() over () - 1 jdx
    FROM (
      SELECT unnest(array_fill(1, array[ny::integer]))
      FROM tbl_ext0
    ) x
  )
  SELECT idx, jdx, 
  xmn + idx * dx x0 , 
  xmn + (idx+1) * dx x1 ,
  ymn + jdx * dy y0 , 
  ymn + (jdx+1) * dy y1 
  FROM foo CROSS JOIN bar, tbl_ext0
)
SELECT *,  
('SRID=4326; POLYGON((' || 
    x0::text || ' '  ||  y0::text || ',' ||
    x0::text || ' '  ||  y1::text || ',' ||
    x1::text || ' '  ||  y1::text || ',' ||
    x1::text || ' '  ||  y0::text || ',' ||
    x0::text || ' '  ||  y0::text || '))')::geometry geom
FROM baz;

DROP TYPE IF EXISTS persistence CASCADE;
CREATE TYPE persistence AS ( 
  grpid integer,
  fireid integer,
  ndetect integer
);
CREATE OR REPLACE FUNCTION find_persistence(tbl regclass)
RETURNS setof persistence 
AS $$ 

-- get table of work_lrg, find list of persistent detection (i.e. collocated detections across days)
DECLARE
  n integer;

BEGIN

  -- subset smaller fires
  DROP TABLE IF EXISTS tbl_pers_in;
  EXECUTE 'CREATE TEMPORARY TABLE tbl_pers_in AS (
    SELECT * from ' || tbl || 
    ' WHERE area_sqkm < 2 ' || 
    ');';
  n := (SELECT count(*) FROM tbl_pers_in);
  raise notice 'pers: in %', n;

  -- create near table
  DROP TABLE IF EXISTS tbl_pers_near;
  CREATE TEMPORARY TABLE tbl_pers_near AS ( 
    WITH foo AS ( 
      SELECT 
      a.fireid AS aid, 
      a.geom_lrg AS ageom, 
      b.fireid AS bid, 
      b.geom_lrg AS bgeom 
      FROM tbl_pers_in AS a 
      INNER JOIN tbl_pers_in AS b 
      ON a.geom_lrg && b.geom_lrg
      AND ST_Overlaps(a.geom_lrg, b.geom_lrg) 
      and a.fireid < b.fireid
    ) 
    SELECT aid AS lhs, bid AS rhs 
    FROM foo) 
  ;
  CREATE UNIQUE INDEX idx_pers_near ON tbl_pers_near(lhs, rhs);
  n := (SELECT count(*) FROM tbl_pers_near);
  raise notice 'pers: near %', n;

  IF n = 0 THEN
    RETURN;
  END IF;

  -- identify connected components
  DROP TABLE IF EXISTS tbl_pers_togrp;
  CREATE TEMPORARY TABLE tbl_pers_togrp AS
  WITH foo AS
  (
    SELECT array_agg(lhs) lhs, array_agg(rhs) rhs
    FROM tbl_pers_near
  ),
  bar AS
  (
    SELECT pnt2grp(lhs, rhs) pnt2grp
    FROM foo
  )
  SELECT (pnt2grp).fireid,  (pnt2grp).lhs, (pnt2grp).rhs, (pnt2grp).ndetect
  FROM bar;

  n := (SELECT count(*) FROM tbl_pers_togrp);
  raise notice 'pers: togrp %', n;

  -- make list of nearby fires with count of repeated obs
  DROP TABLE IF EXISTS tbl_pers_grpcnt;
  CREATE TEMPORARY TABLE tbl_pers_grpcnt AS
  with foo AS (
    SELECT fireid,lhs,ndetect FROM tbl_pers_togrp
    UNION ALL 
    SELECT fireid,rhs,ndetect FROM tbl_pers_togrp
  )
  SELECT DISTINCT fireid grpid, lhs fireid, ndetect FROM foo;

  n := (SELECT count(*) FROM tbl_pers_grpcnt);
  raise notice 'pers: grpcnt %', n;

  RETURN QUERY SELECT grpid, fireid, ndetect FROM tbl_pers_grpcnt;


  RETURN;

END
$$ LANGUAGE plpgsql;



DO language plpgsql $$
  DECLARE
    r RECORD;
--    idx INTEGER := 0;
--    jdx INTEGER := 0;
--    nx INTEGER;
--    ny INTEGER;
        n INTEGER;
        p persistence[];
  BEGIN

    DROP TABLE IF EXISTS tbl_persistent;
    CREATE TABLE tbl_persistent (
      grpid  integer,
      fireid integer,
      ndetect integer
    );



    FOR r IN SELECT * FROM tbl_ext LOOP 
      --raise notice '% % %', r.idx, r.jdx,  ST_AsText(r.geom); 

      --IF r.idx = 4 AND r.jdx = 2 THEN -- Texas?
      --IF r.idx = 4 AND r.jdx = 0 THEN -- only 1000 points
--      IF r.x0 < -98.1 AND r.x1 > -98.1 AND r.y0 < 29.7 AND r.y1 > 29.7 OR -- New Braunfels
--        r.x0 < -99.3 AND r.x1 > -99.3 AND r.y0 < 28.3 AND r.y1 > 28.3 -- Artesia Wells
        IF TRUE
        THEN 

      -- create scratch table
      DROP TABLE IF EXISTS tbl_dupdet;
      CREATE TEMPORARY TABLE tbl_dupdet AS
      ( 
        SELECT * FROM work_lrg 
        WHERE 
        work_lrg.geom_lrg && r.geom 
        AND st_intersects(work_lrg.geom_lrg, r.geom)
      ); 
      
      n:= (select count(*) from tbl_dupdet);
      raise notice '% % % %', r.idx, r.jdx, n, ST_AsText(r.geom);

      CONTINUE WHEN n < 2;


      WITH foo AS (
        SELECT find_persistence('tbl_dupdet') x
      )
      INSERT INTO tbl_persistent
      SELECT (x).grpid, (x).fireid, (x).ndetect 
      FROM foo;



      END IF;


--
    END LOOP;
END
$$ ;











-- vim: et sw=2