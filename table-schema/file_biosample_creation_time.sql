SELECT DISTINCT
  fdb.file_id_namespace,
  fdb.file_id,
  b.creation_time AS biosample_creation_time
FROM file_describes_biosample fdb
JOIN biosample b
  ON (fdb.biosample_id_namespace = b.id_namespace AND fdb.biosample_id = b.id)
WHERE b.creation_time IS NOT NULL
