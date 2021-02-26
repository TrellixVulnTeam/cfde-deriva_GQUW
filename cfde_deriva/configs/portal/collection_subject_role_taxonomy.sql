INSERT INTO collection_subject_role_taxonomy (
  collection_id_namespace,
  collection_local_id,
  subject_role_id,
  subject_taxonomy_id
)
SELECT
  cg.leader_collection_id_namespace AS collection_id_namespace,
  cg.leader_collection_local_id AS collection_local_id,
  fsrt.subject_role_id,
  fsrt.subject_taxonomy_id
FROM collection_in_collection_transitive cg
JOIN file_in_collection fic
  ON (cg.member_collection_id_namespace = fic.collection_id_namespace AND cg.member_collection_local_id = fic.collection_local_id)
JOIN file_subject_role_taxonomy fsrt
  ON (fic.file_id_namespace = fsrt.file_id_namespace AND fic.file_local_id = fsrt.file_local_id)

UNION

SELECT DISTINCT
  cg.leader_collection_id_namespace AS collection_id_namespace,
  cg.leader_collection_local_id AS collection_local_id,
  srt.role_id AS subject_role_id,
  srt.taxonomy_id AS subject_taxonomy_id
FROM collection_in_collection_transitive cg
JOIN subject_in_collection sic
  ON (cg.member_collection_id_namespace = sic.collection_id_namespace AND cg.member_collection_local_id = sic.collection_local_id)
JOIN subject_role_taxonomy srt
  ON (sic.subject_id_namespace = srt.subject_id_namespace AND sic.subject_local_id = srt.subject_local_id)

UNION

SELECT DISTINCT
  cg.leader_collection_id_namespace AS collection_id_namespace,
  cg.leader_collection_local_id AS collection_local_id,
  srt.role_id AS subject_role_id,
  srt.taxonomy_id AS subject_taxonomy_id
FROM collection_in_collection_transitive cg
JOIN biosample_in_collection bic
  ON (cg.member_collection_id_namespace = bic.collection_id_namespace AND cg.member_collection_local_id = bic.collection_local_id)
JOIN biosample_from_subject bfs
  ON (bic.biosample_id_namespace = bfs.biosample_id_namespace AND bic.biosample_local_id = bfs.biosample_local_id)
JOIN subject_role_taxonomy srt
  ON (bfs.subject_id_namespace = srt.subject_id_namespace AND bfs.subject_local_id = srt.subject_local_id)
;
