#!/usr/bin/env python
"""The MySQL database methods for client handling."""
import itertools
from typing import Collection, Iterator, List, Mapping, Optional, Text

import MySQLdb
from MySQLdb.constants import ER as mysql_error_constants
import MySQLdb.cursors

from grr_response_core.lib import rdfvalue
from grr_response_core.lib.rdfvalues import client as rdf_client
from grr_response_core.lib.rdfvalues import client_network as rdf_client_network
from grr_response_core.lib.rdfvalues import client_stats as rdf_client_stats
from grr_response_core.lib.rdfvalues import crypto as rdf_crypto
from grr_response_core.lib.rdfvalues import search as rdf_search
from grr_response_server import fleet_utils
from grr_response_server.databases import db
from grr_response_server.databases import db_utils
from grr_response_server.databases import mysql_utils
from grr_response_server.rdfvalues import objects as rdf_objects
from grr_response_server.rdfvalues import rrg as rdf_rrg
from grr_response_proto.rrg import startup_pb2 as rrg_startup_pb2


class MySQLDBClientMixin(object):
  """MySQLDataStore mixin for client related functions."""

  @mysql_utils.WithTransaction()
  def WriteClientMetadata(
      self,
      client_id: str,
      certificate: Optional[rdf_crypto.RDFX509Cert] = None,
      first_seen: Optional[rdfvalue.RDFDatetime] = None,
      last_ping: Optional[rdfvalue.RDFDatetime] = None,
      last_clock: Optional[rdfvalue.RDFDatetime] = None,
      last_ip: Optional[rdf_client_network.NetworkAddress] = None,
      last_foreman: Optional[rdfvalue.RDFDatetime] = None,
      fleetspeak_validation_info: Optional[Mapping[str, str]] = None,
      cursor: Optional[MySQLdb.cursors.Cursor] = None,
  ) -> None:
    """Write metadata about the client."""
    placeholders = []
    values = dict()

    placeholders.append("%(client_id)s")
    values["client_id"] = db_utils.ClientIDToInt(client_id)

    if certificate:
      placeholders.append("%(certificate)s")
      values["certificate"] = certificate.SerializeToBytes()
    if first_seen is not None:
      placeholders.append("FROM_UNIXTIME(%(first_seen)s)")
      values["first_seen"] = mysql_utils.RDFDatetimeToTimestamp(first_seen)
    if last_ping is not None:
      placeholders.append("FROM_UNIXTIME(%(last_ping)s)")
      values["last_ping"] = mysql_utils.RDFDatetimeToTimestamp(last_ping)
    if last_clock:
      placeholders.append("FROM_UNIXTIME(%(last_clock)s)")
      values["last_clock"] = mysql_utils.RDFDatetimeToTimestamp(last_clock)
    if last_ip:
      placeholders.append("%(last_ip)s")
      values["last_ip"] = last_ip.SerializeToBytes()
    if last_foreman:
      placeholders.append("FROM_UNIXTIME(%(last_foreman)s)")
      values["last_foreman"] = mysql_utils.RDFDatetimeToTimestamp(last_foreman)

    placeholders.append("%(last_fleetspeak_validation_info)s")
    if fleetspeak_validation_info:
      pb = rdf_client.FleetspeakValidationInfo.FromStringDict(
          fleetspeak_validation_info)
      values["last_fleetspeak_validation_info"] = pb.SerializeToBytes()
    else:
      # Write null for empty or non-existent validation info.
      values["last_fleetspeak_validation_info"] = None

    updates = []
    for column in values:
      updates.append("{column} = VALUES({column})".format(column=column))

    query = """
    INSERT INTO clients ({columns})
    VALUES ({placeholders})
    ON DUPLICATE KEY UPDATE {updates}
    """.format(
        columns=", ".join(values.keys()),
        placeholders=", ".join(placeholders),
        updates=", ".join(updates))

    cursor.execute(query, values)

  @mysql_utils.WithTransaction(readonly=True)
  def MultiReadClientMetadata(self, client_ids, cursor=None):
    """Reads ClientMetadata records for a list of clients."""
    ids = [db_utils.ClientIDToInt(client_id) for client_id in client_ids]
    query = """
      SELECT
        client_id,
        certificate,
        UNIX_TIMESTAMP(last_ping),
        UNIX_TIMESTAMP(last_clock),
        last_ip,
        UNIX_TIMESTAMP(last_foreman),
        UNIX_TIMESTAMP(first_seen),
        UNIX_TIMESTAMP(last_crash_timestamp),
        UNIX_TIMESTAMP(last_startup_timestamp),
        last_fleetspeak_validation_info
      FROM
        clients
      WHERE
        client_id IN ({})""".format(", ".join(["%s"] * len(ids)))
    ret = {}
    cursor.execute(query, ids)
    while True:
      row = cursor.fetchone()
      if not row:
        break
      cid, crt, ping, clk, ip, foreman, first, lct, lst, fsvi = row
      metadata = rdf_objects.ClientMetadata(
          certificate=crt,
          first_seen=mysql_utils.TimestampToRDFDatetime(first),
          ping=mysql_utils.TimestampToRDFDatetime(ping),
          clock=mysql_utils.TimestampToRDFDatetime(clk),
          ip=mysql_utils.StringToRDFProto(
              rdf_client_network.NetworkAddress, ip
          ),
          last_foreman_time=mysql_utils.TimestampToRDFDatetime(foreman),
          startup_info_timestamp=mysql_utils.TimestampToRDFDatetime(lst),
          last_crash_timestamp=mysql_utils.TimestampToRDFDatetime(lct),
      )

      if fsvi:
        metadata.last_fleetspeak_validation_info = (
            rdf_client.FleetspeakValidationInfo.FromSerializedBytes(fsvi))

      ret[db_utils.IntToClientID(cid)] = metadata

    return ret

  @mysql_utils.WithTransaction()
  def WriteClientSnapshot(self, snapshot, cursor=None):
    """Write new client snapshot."""
    cursor.execute("SET @now = NOW(6)")

    insert_history_query = (
        "INSERT INTO client_snapshot_history(client_id, timestamp, "
        "client_snapshot) VALUES (%s, @now, %s)")
    insert_startup_query = (
        "INSERT INTO client_startup_history(client_id, timestamp, "
        "startup_info) VALUES(%s, @now, %s)")

    client_info = {
        "last_version_string": snapshot.GetGRRVersionString(),
        "last_platform": snapshot.knowledge_base.os,
        "last_platform_release": snapshot.Uname(),
    }
    update_clauses = [
        "last_snapshot_timestamp = @now",
        "last_startup_timestamp = @now",
        "last_version_string = %(last_version_string)s",
        "last_platform = %(last_platform)s",
        "last_platform_release = %(last_platform_release)s",
    ]

    update_query = (
        "UPDATE clients SET {} WHERE client_id = %(client_id)s".format(
            ", ".join(update_clauses)))

    int_client_id = db_utils.ClientIDToInt(snapshot.client_id)
    client_info["client_id"] = int_client_id

    startup_info = snapshot.startup_info
    snapshot.startup_info = None
    try:
      cursor.execute(insert_history_query,
                     (int_client_id, snapshot.SerializeToBytes()))
      cursor.execute(insert_startup_query,
                     (int_client_id, startup_info.SerializeToBytes()))
      cursor.execute(update_query, client_info)
    except MySQLdb.IntegrityError as e:
      if e.args and e.args[0] == mysql_error_constants.NO_REFERENCED_ROW_2:
        raise db.UnknownClientError(snapshot.client_id, cause=e)
      else:
        raise
    finally:
      snapshot.startup_info = startup_info

  @mysql_utils.WithTransaction(readonly=True)
  def MultiReadClientSnapshot(self, client_ids, cursor=None):
    """Reads the latest client snapshots for a list of clients."""
    if not client_ids:
      return {}

    int_ids = [db_utils.ClientIDToInt(cid) for cid in client_ids]
    query = (
        "SELECT h.client_id, h.client_snapshot, UNIX_TIMESTAMP(h.timestamp),"
        "       s.startup_info "
        "FROM clients as c FORCE INDEX (PRIMARY), "
        "client_snapshot_history as h FORCE INDEX (PRIMARY), "
        "client_startup_history as s FORCE INDEX (PRIMARY) "
        "WHERE h.client_id = c.client_id "
        "AND s.client_id = c.client_id "
        "AND h.timestamp = c.last_snapshot_timestamp "
        "AND s.timestamp = c.last_startup_timestamp "
        "AND c.client_id IN ({})").format(", ".join(["%s"] * len(client_ids)))
    ret = {cid: None for cid in client_ids}
    cursor.execute(query, int_ids)

    while True:
      row = cursor.fetchone()
      if not row:
        break
      cid, snapshot, timestamp, startup_info = row
      client_obj = mysql_utils.StringToRDFProto(rdf_objects.ClientSnapshot,
                                                snapshot)
      client_obj.startup_info = mysql_utils.StringToRDFProto(
          rdf_client.StartupInfo, startup_info)
      client_obj.timestamp = mysql_utils.TimestampToRDFDatetime(timestamp)
      ret[db_utils.IntToClientID(cid)] = client_obj
    return ret

  @mysql_utils.WithTransaction(readonly=True)
  def ReadClientSnapshotHistory(self, client_id, timerange=None, cursor=None):
    """Reads the full history for a particular client."""

    client_id_int = db_utils.ClientIDToInt(client_id)

    query = ("SELECT sn.client_snapshot, st.startup_info, "
             "       UNIX_TIMESTAMP(sn.timestamp) FROM "
             "client_snapshot_history AS sn, "
             "client_startup_history AS st WHERE "
             "sn.client_id = st.client_id AND "
             "sn.timestamp = st.timestamp AND "
             "sn.client_id=%s ")

    args = [client_id_int]
    if timerange:
      time_from, time_to = timerange  # pylint: disable=unpacking-non-sequence

      if time_from is not None:
        query += "AND sn.timestamp >= FROM_UNIXTIME(%s) "
        args.append(mysql_utils.RDFDatetimeToTimestamp(time_from))

      if time_to is not None:
        query += "AND sn.timestamp <= FROM_UNIXTIME(%s) "
        args.append(mysql_utils.RDFDatetimeToTimestamp(time_to))

    query += "ORDER BY sn.timestamp DESC"

    ret = []
    cursor.execute(query, args)
    for snapshot, startup_info, timestamp in cursor.fetchall():
      client = rdf_objects.ClientSnapshot.FromSerializedBytes(snapshot)
      client.startup_info = rdf_client.StartupInfo.FromSerializedBytes(
          startup_info)
      client.timestamp = mysql_utils.TimestampToRDFDatetime(timestamp)

      ret.append(client)
    return ret

  @mysql_utils.WithTransaction()
  def WriteClientSnapshotHistory(self, clients, cursor=None):
    """Writes the full history for a particular client."""
    client_id = clients[0].client_id
    latest_timestamp = max(client.timestamp for client in clients)

    base_params = {
        "client_id": db_utils.ClientIDToInt(client_id),
        "latest_timestamp": mysql_utils.RDFDatetimeToTimestamp(latest_timestamp)
    }

    try:
      for client in clients:
        startup_info = client.startup_info
        client.startup_info = None

        params = base_params.copy()
        params.update({
            "timestamp": mysql_utils.RDFDatetimeToTimestamp(client.timestamp),
            "client_snapshot": client.SerializeToBytes(),
            "startup_info": startup_info.SerializeToBytes(),
        })

        cursor.execute(
            """
        INSERT INTO client_snapshot_history (client_id, timestamp,
                                             client_snapshot)
        VALUES (%(client_id)s, FROM_UNIXTIME(%(timestamp)s),
                %(client_snapshot)s)
        """, params)

        cursor.execute(
            """
        INSERT INTO client_startup_history (client_id, timestamp,
                                            startup_info)
        VALUES (%(client_id)s, FROM_UNIXTIME(%(timestamp)s),
                %(startup_info)s)
        """, params)

        client.startup_info = startup_info

      cursor.execute(
          """
      UPDATE clients
         SET last_snapshot_timestamp = FROM_UNIXTIME(%(latest_timestamp)s)
       WHERE client_id = %(client_id)s
         AND (last_snapshot_timestamp IS NULL OR
              last_snapshot_timestamp < FROM_UNIXTIME(%(latest_timestamp)s))
      """, base_params)

      cursor.execute(
          """
      UPDATE clients
         SET last_startup_timestamp = FROM_UNIXTIME(%(latest_timestamp)s)
       WHERE client_id = %(client_id)s
         AND (last_startup_timestamp IS NULL OR
              last_startup_timestamp < FROM_UNIXTIME(%(latest_timestamp)s))
      """, base_params)
    except MySQLdb.IntegrityError as error:
      raise db.UnknownClientError(client_id, cause=error)

  @mysql_utils.WithTransaction()
  def WriteClientStartupInfo(self, client_id, startup_info, cursor=None):
    """Writes a new client startup record."""
    cursor.execute("SET @now = NOW(6)")

    params = {
        "client_id": db_utils.ClientIDToInt(client_id),
        "startup_info": startup_info.SerializeToBytes(),
    }

    try:
      cursor.execute(
          """
      INSERT INTO client_startup_history
        (client_id, timestamp, startup_info)
      VALUES
        (%(client_id)s, @now, %(startup_info)s)
          """, params)

      cursor.execute(
          """
      UPDATE clients
         SET last_startup_timestamp = @now
       WHERE client_id = %(client_id)s
      """, params)
    except MySQLdb.IntegrityError as e:
      raise db.UnknownClientError(client_id, cause=e)

  @mysql_utils.WithTransaction()
  def WriteClientRRGStartup(
      self,
      client_id: str,
      startup: rrg_startup_pb2.Startup,
      cursor: Optional[MySQLdb.cursors.Cursor] = None,
  ) -> None:
    """Writes a new RRG startup entry to the database."""
    query = """
    INSERT
      INTO client_rrg_startup_history (client_id, timestamp, startup)
    VALUES (%(client_id)s, NOW(6), %(startup)s)
    """
    params = {
        "client_id": db_utils.ClientIDToInt(client_id),
        "startup": startup.SerializeToString(),
    }

    try:
      cursor.execute(query, params)
    except MySQLdb.IntegrityError as error:
      raise db.UnknownClientError(client_id) from error

  @mysql_utils.WithTransaction()
  def ReadClientRRGStartup(
      self,
      client_id: str,
      cursor: Optional[MySQLdb.cursors.Cursor] = None,
  ) -> Optional[rrg_startup_pb2.Startup]:
    """Reads the latest RRG startup entry for the given client."""
    query = """
    SELECT su.startup
      FROM clients
           LEFT JOIN (SELECT startup
                        FROM client_rrg_startup_history
                       WHERE client_id = %(client_id)s
                    ORDER BY timestamp DESC
                       LIMIT 1) AS su
                     ON TRUE
     WHERE client_id = %(client_id)s
    """
    params = {
        "client_id": db_utils.ClientIDToInt(client_id),
    }

    cursor.execute(query, params)

    row = cursor.fetchone()
    if row is None:
      raise db.UnknownClientError(client_id)

    (startup_bytes,) = row
    if startup_bytes is None:
      return None

    return rrg_startup_pb2.Startup.FromString(startup_bytes)

  @mysql_utils.WithTransaction(readonly=True)
  def ReadClientStartupInfo(
      self,
      client_id: str,
      cursor: Optional[MySQLdb.cursors.Cursor] = None
  ) -> Optional[rdf_client.StartupInfo]:
    """Reads the latest client startup record for a single client."""
    query = """
    SELECT startup_info, UNIX_TIMESTAMP(timestamp)
      FROM clients, client_startup_history
     WHERE clients.last_startup_timestamp = client_startup_history.timestamp
       AND clients.client_id = client_startup_history.client_id
       AND clients.client_id = %(client_id)s
    """
    params = {
        "client_id": db_utils.ClientIDToInt(client_id),
    }
    cursor.execute(query, params)

    row = cursor.fetchone()
    if row is None:
      return None

    startup_info, timestamp = row
    res = rdf_client.StartupInfo.FromSerializedBytes(startup_info)
    res.timestamp = mysql_utils.TimestampToRDFDatetime(timestamp)
    return res

  @mysql_utils.WithTransaction(readonly=True)
  def ReadClientStartupInfoHistory(self,
                                   client_id,
                                   timerange=None,
                                   cursor=None):
    """Reads the full startup history for a particular client."""

    client_id_int = db_utils.ClientIDToInt(client_id)

    query = ("SELECT startup_info, UNIX_TIMESTAMP(timestamp) "
             "FROM client_startup_history "
             "WHERE client_id=%s ")
    args = [client_id_int]

    if timerange:
      time_from, time_to = timerange  # pylint: disable=unpacking-non-sequence

      if time_from is not None:
        query += "AND timestamp >= FROM_UNIXTIME(%s) "
        args.append(mysql_utils.RDFDatetimeToTimestamp(time_from))

      if time_to is not None:
        query += "AND timestamp <= FROM_UNIXTIME(%s) "
        args.append(mysql_utils.RDFDatetimeToTimestamp(time_to))

    query += "ORDER BY timestamp DESC "

    ret = []
    cursor.execute(query, args)

    for startup_info, timestamp in cursor.fetchall():
      si = rdf_client.StartupInfo.FromSerializedBytes(startup_info)
      si.timestamp = mysql_utils.TimestampToRDFDatetime(timestamp)
      ret.append(si)
    return ret

  def _ResponseToClientsFullInfo(self, response):
    """Creates a ClientFullInfo object from a database response."""
    c_full_info = None
    prev_cid = None
    for row in response:
      (
          cid,
          crt,
          ip,
          ping,
          clk,
          foreman,
          first,
          last_client_ts,
          last_crash_ts,
          last_startup_ts,
          client_obj,
          client_startup_obj,
          last_startup_obj,
          last_rrg_startup_obj,
          label_owner,
          label_name,
      ) = row

      if cid != prev_cid:
        if c_full_info:
          yield db_utils.IntToClientID(prev_cid), c_full_info

        metadata = rdf_objects.ClientMetadata(
            certificate=crt,
            first_seen=mysql_utils.TimestampToRDFDatetime(first),
            ping=mysql_utils.TimestampToRDFDatetime(ping),
            clock=mysql_utils.TimestampToRDFDatetime(clk),
            ip=mysql_utils.StringToRDFProto(rdf_client_network.NetworkAddress,
                                            ip),
            last_foreman_time=mysql_utils.TimestampToRDFDatetime(foreman),
            startup_info_timestamp=mysql_utils.TimestampToRDFDatetime(
                last_startup_ts),
            last_crash_timestamp=mysql_utils.TimestampToRDFDatetime(
                last_crash_ts))

        if client_obj is not None:
          l_snapshot = rdf_objects.ClientSnapshot.FromSerializedBytes(
              client_obj)
          l_snapshot.timestamp = mysql_utils.TimestampToRDFDatetime(
              last_client_ts)
          l_snapshot.startup_info = rdf_client.StartupInfo.FromSerializedBytes(
              client_startup_obj)
          l_snapshot.startup_info.timestamp = l_snapshot.timestamp
        else:
          l_snapshot = rdf_objects.ClientSnapshot(
              client_id=db_utils.IntToClientID(cid))

        if last_startup_obj is not None:
          startup_info = rdf_client.StartupInfo.FromSerializedBytes(
              last_startup_obj)
          startup_info.timestamp = mysql_utils.TimestampToRDFDatetime(
              last_startup_ts)
        else:
          startup_info = None

        if last_rrg_startup_obj is not None:
          last_rrg_startup = rdf_rrg.Startup.FromSerializedBytes(
              last_rrg_startup_obj,
          )
        else:
          last_rrg_startup = None

        prev_cid = cid
        c_full_info = rdf_objects.ClientFullInfo(
            metadata=metadata,
            labels=[],
            last_snapshot=l_snapshot,
            last_startup_info=startup_info,
            last_rrg_startup=last_rrg_startup,
        )

      if label_owner and label_name:
        c_full_info.labels.append(
            rdf_objects.ClientLabel(name=label_name, owner=label_owner))

    if c_full_info:
      yield db_utils.IntToClientID(prev_cid), c_full_info

  @mysql_utils.WithTransaction(readonly=True)
  def MultiReadClientFullInfo(self,
                              client_ids,
                              min_last_ping=None,
                              cursor=None):
    """Reads full client information for a list of clients."""
    if not client_ids:
      return {}

    query = """
    SELECT c.client_id, c.certificate, c.last_ip,
           UNIX_TIMESTAMP(c.last_ping),
           UNIX_TIMESTAMP(c.last_clock),
           UNIX_TIMESTAMP(c.last_foreman),
           UNIX_TIMESTAMP(c.first_seen),
           UNIX_TIMESTAMP(c.last_snapshot_timestamp),
           UNIX_TIMESTAMP(c.last_crash_timestamp),
           UNIX_TIMESTAMP(c.last_startup_timestamp),
           h.client_snapshot,
           s.startup_info, s_last.startup_info, rrg_s_last.startup,
           l.owner_username, l.label
      FROM clients AS c FORCE INDEX (PRIMARY)
           LEFT JOIN client_snapshot_history AS h FORCE INDEX (PRIMARY)
                  ON c.client_id = h.client_id
                 AND c.last_snapshot_timestamp = h.timestamp
           LEFT JOIN client_startup_history AS s FORCE INDEX (PRIMARY)
                  ON c.client_id = s.client_id
                 AND c.last_snapshot_timestamp = s.timestamp
           LEFT JOIN client_startup_history AS s_last FORCE INDEX (PRIMARY)
                  ON c.client_id = s_last.client_id
                 AND c.last_startup_timestamp = s_last.timestamp
           LEFT JOIN client_rrg_startup_history AS rrg_s_last
                  ON rrg_s_last.id = (SELECT id
                                        FROM client_rrg_startup_history
                                       WHERE client_id = c.client_id
                                    ORDER BY timestamp DESC
                                       LIMIT 1)
           LEFT JOIN client_labels AS l FORCE INDEX (PRIMARY)
                  ON c.client_id = l.client_id
    """

    query += "WHERE c.client_id IN (%s) " % ", ".join(["%s"] * len(client_ids))

    values = [db_utils.ClientIDToInt(cid) for cid in client_ids]
    if min_last_ping is not None:
      query += "AND c.last_ping >= FROM_UNIXTIME(%s)"
      values.append(mysql_utils.RDFDatetimeToTimestamp(min_last_ping))

    cursor.execute(query, values)
    return dict(self._ResponseToClientsFullInfo(cursor.fetchall()))

  def ReadClientLastPings(self,
                          min_last_ping=None,
                          max_last_ping=None,
                          batch_size=db.CLIENT_IDS_BATCH_SIZE):
    """Yields dicts of last-ping timestamps for clients in the DB."""
    last_client_id = db_utils.IntToClientID(0)

    while True:
      last_client_id, last_pings = self._ReadClientLastPings(
          last_client_id,
          batch_size,
          min_last_ping=min_last_ping,
          max_last_ping=max_last_ping,
      )
      if last_pings:
        yield last_pings
      if len(last_pings) < batch_size:
        break

  @mysql_utils.WithTransaction(readonly=True)
  def _ReadClientLastPings(self,
                           last_client_id,
                           count,
                           min_last_ping=None,
                           max_last_ping=None,
                           cursor=None):
    """Yields dicts of last-ping timestamps for clients in the DB."""
    where_filters = ["client_id > %s"]
    query_values = [db_utils.ClientIDToInt(last_client_id)]
    if min_last_ping is not None:
      where_filters.append("last_ping >= FROM_UNIXTIME(%s) ")
      query_values.append(mysql_utils.RDFDatetimeToTimestamp(min_last_ping))
    if max_last_ping is not None:
      where_filters.append(
          "(last_ping IS NULL OR last_ping <= FROM_UNIXTIME(%s))")
      query_values.append(mysql_utils.RDFDatetimeToTimestamp(max_last_ping))

    query = """
      SELECT client_id, UNIX_TIMESTAMP(last_ping)
      FROM clients
      WHERE {}
      ORDER BY client_id
      LIMIT %s""".format(" AND ".join(where_filters))

    cursor.execute(query, query_values + [count])
    last_pings = {}
    last_client_id = None
    for int_client_id, last_ping in cursor.fetchall():
      last_client_id = db_utils.IntToClientID(int_client_id)
      last_pings[last_client_id] = mysql_utils.TimestampToRDFDatetime(last_ping)
    return last_client_id, last_pings

  @mysql_utils.WithTransaction()
  def MultiAddClientKeywords(
      self,
      client_ids: Collection[str],
      keywords: Collection[str],
      cursor: Optional[MySQLdb.cursors.Cursor] = None,
  ) -> None:
    """Associates the provided keywords with the specified clients."""
    # Early return to avoid generating invalid SQL code.
    if not client_ids or not keywords:
      return

    args = []

    for client_id in client_ids:
      int_client_id = db_utils.ClientIDToInt(client_id)
      for keyword in keywords:
        keyword_hash = mysql_utils.Hash(keyword)
        args.append((int_client_id, keyword_hash, keyword))

    query = """
        INSERT INTO client_keywords (client_id, keyword_hash, keyword)
        VALUES {}
        ON DUPLICATE KEY UPDATE timestamp = NOW(6)
            """.format(
        ", ".join(["(%s, %s, %s)"] * len(args))
    )
    try:
      cursor.execute(query, list(itertools.chain.from_iterable(args)))
    except MySQLdb.IntegrityError as error:
      raise db.AtLeastOneUnknownClientError(client_ids) from error

  @mysql_utils.WithTransaction()
  def RemoveClientKeyword(self, client_id, keyword, cursor=None):
    """Removes the association of a particular client to a keyword."""
    cursor.execute(
        "DELETE FROM client_keywords "
        "WHERE client_id = %s AND keyword_hash = %s",
        [db_utils.ClientIDToInt(client_id),
         mysql_utils.Hash(keyword)])

  @mysql_utils.WithTransaction(readonly=True)
  def ListClientsForKeywords(self, keywords, start_time=None, cursor=None):
    """Lists the clients associated with keywords."""
    keywords = set(keywords)
    hash_to_kw = {mysql_utils.Hash(kw): kw for kw in keywords}
    result = {kw: [] for kw in keywords}

    query = """
      SELECT keyword_hash, client_id
      FROM client_keywords
      FORCE INDEX (client_index_by_keyword_hash)
      WHERE keyword_hash IN ({})
    """.format(", ".join(["%s"] * len(result)))
    args = list(hash_to_kw.keys())
    if start_time:
      query += " AND timestamp >= FROM_UNIXTIME(%s)"
      args.append(mysql_utils.RDFDatetimeToTimestamp(start_time))
    cursor.execute(query, args)

    for kw_hash, cid in cursor.fetchall():
      result[hash_to_kw[kw_hash]].append(db_utils.IntToClientID(cid))
    return result

  @mysql_utils.WithTransaction()
  def MultiAddClientLabels(
      self,
      client_ids: Collection[str],
      owner: str,
      labels: Collection[str],
      cursor: Optional[MySQLdb.cursors.Cursor] = None,
  ) -> None:
    """Attaches user labels to the specified clients."""
    # Early return to avoid generating invalid SQL code.
    if not client_ids or not labels:
      return

    args = []
    for client_id in client_ids:
      client_id_int = db_utils.ClientIDToInt(client_id)
      owner_hash = mysql_utils.Hash(owner)

      for label in labels:
        args.append((client_id_int, owner_hash, owner, label))

    query = f"""
     INSERT
     IGNORE
       INTO client_labels
            (client_id, owner_username_hash, owner_username, label)
     VALUES {", ".join(["(%s, %s, %s, %s)"] * len(args))}
    """

    args = list(itertools.chain.from_iterable(args))
    try:
      cursor.execute(query, args)
    except MySQLdb.IntegrityError as error:
      raise db.AtLeastOneUnknownClientError(client_ids) from error

  @mysql_utils.WithTransaction(readonly=True)
  def MultiReadClientLabels(self, client_ids, cursor=None):
    """Reads the user labels for a list of clients."""

    int_ids = [db_utils.ClientIDToInt(cid) for cid in client_ids]
    query = ("SELECT client_id, owner_username, label "
             "FROM client_labels "
             "WHERE client_id IN ({})").format(", ".join(["%s"] *
                                                         len(client_ids)))

    ret = {client_id: [] for client_id in client_ids}
    cursor.execute(query, int_ids)
    for client_id, owner, label in cursor.fetchall():
      ret[db_utils.IntToClientID(client_id)].append(
          rdf_objects.ClientLabel(name=label, owner=owner))

    for r in ret.values():
      r.sort(key=lambda label: (label.owner, label.name))
    return ret

  @mysql_utils.WithTransaction()
  def RemoveClientLabels(self, client_id, owner, labels, cursor=None):
    """Removes a list of user labels from a given client."""

    query = ("DELETE FROM client_labels "
             "WHERE client_id = %s AND owner_username_hash = %s "
             "AND label IN ({})").format(", ".join(["%s"] * len(labels)))
    args = itertools.chain([
        db_utils.ClientIDToInt(client_id),
        mysql_utils.Hash(owner),
    ], labels)
    cursor.execute(query, args)

  @mysql_utils.WithTransaction(readonly=True)
  def ReadAllClientLabels(self, cursor=None):
    """Reads the user labels for a list of clients."""

    cursor.execute("SELECT DISTINCT label FROM client_labels")

    result = []
    for (label,) in cursor.fetchall():
      result.append(label)

    return result

  @mysql_utils.WithTransaction()
  def WriteClientCrashInfo(self, client_id, crash_info, cursor=None):
    """Writes a new client crash record."""
    cursor.execute("SET @now = NOW(6)")

    params = {
        "client_id": db_utils.ClientIDToInt(client_id),
        "crash_info": crash_info.SerializeToBytes(),
    }

    try:
      cursor.execute(
          """
      INSERT INTO client_crash_history (client_id, timestamp, crash_info)
           VALUES (%(client_id)s, @now, %(crash_info)s)
      """, params)

      cursor.execute(
          """
      UPDATE clients
         SET last_crash_timestamp = @now
       WHERE client_id = %(client_id)s
      """, params)

    except MySQLdb.IntegrityError as e:
      raise db.UnknownClientError(client_id, cause=e)

  @mysql_utils.WithTransaction(readonly=True)
  def ReadClientCrashInfo(self, client_id, cursor=None):
    """Reads the latest client crash record for a single client."""
    cursor.execute(
        "SELECT UNIX_TIMESTAMP(timestamp), crash_info "
        "FROM clients, client_crash_history WHERE "
        "clients.client_id = client_crash_history.client_id AND "
        "clients.last_crash_timestamp = client_crash_history.timestamp AND "
        "clients.client_id = %s", [db_utils.ClientIDToInt(client_id)])
    row = cursor.fetchone()
    if not row:
      return None

    timestamp, crash_info = row
    res = rdf_client.ClientCrash.FromSerializedBytes(crash_info)
    res.timestamp = mysql_utils.TimestampToRDFDatetime(timestamp)
    return res

  @mysql_utils.WithTransaction(readonly=True)
  def ReadClientCrashInfoHistory(self, client_id, cursor=None):
    """Reads the full crash history for a particular client."""
    cursor.execute(
        "SELECT UNIX_TIMESTAMP(timestamp), crash_info "
        "FROM client_crash_history WHERE "
        "client_crash_history.client_id = %s "
        "ORDER BY timestamp DESC", [db_utils.ClientIDToInt(client_id)])
    ret = []
    for timestamp, crash_info in cursor.fetchall():
      ci = rdf_client.ClientCrash.FromSerializedBytes(crash_info)
      ci.timestamp = mysql_utils.TimestampToRDFDatetime(timestamp)
      ret.append(ci)
    return ret

  @mysql_utils.WithTransaction()
  def WriteClientStats(self,
                       client_id: Text,
                       stats: rdf_client_stats.ClientStats,
                       cursor=None) -> None:
    """Stores a ClientStats instance."""

    if stats.timestamp is None:
      stats.timestamp = rdfvalue.RDFDatetime.Now()

    try:
      cursor.execute(
          """
          INSERT INTO client_stats (client_id, payload, timestamp)
          VALUES (%s, %s, FROM_UNIXTIME(%s))
          ON DUPLICATE KEY UPDATE payload=VALUES(payload)
          """, [
              db_utils.ClientIDToInt(client_id),
              stats.SerializeToBytes(),
              mysql_utils.RDFDatetimeToTimestamp(stats.timestamp)
          ])
    except MySQLdb.IntegrityError as e:
      if e.args[0] == mysql_error_constants.NO_REFERENCED_ROW_2:
        raise db.UnknownClientError(client_id, cause=e)
      else:
        raise

  @mysql_utils.WithTransaction(readonly=True)
  def ReadClientStats(self,
                      client_id: Text,
                      min_timestamp: rdfvalue.RDFDatetime,
                      max_timestamp: rdfvalue.RDFDatetime,
                      cursor=None) -> List[rdf_client_stats.ClientStats]:
    """Reads ClientStats for a given client and time range."""

    cursor.execute(
        """
        SELECT payload FROM client_stats
        WHERE client_id = %s
          AND timestamp BETWEEN FROM_UNIXTIME(%s) AND FROM_UNIXTIME(%s)
        ORDER BY timestamp ASC
        """, [
            db_utils.ClientIDToInt(client_id),
            mysql_utils.RDFDatetimeToTimestamp(min_timestamp),
            mysql_utils.RDFDatetimeToTimestamp(max_timestamp)
        ])
    return [
        rdf_client_stats.ClientStats.FromSerializedBytes(stats_bytes)
        for stats_bytes, in cursor.fetchall()
    ]

  # DeleteOldClientStats does not use a single transaction, since it runs for
  # a long time. Instead, it uses multiple transactions internally.
  def DeleteOldClientStats(
      self,
      cutoff_time: rdfvalue.RDFDatetime,
      batch_size: Optional[int] = None,
  ) -> Iterator[int]:
    """Deletes client stats older than the specified cutoff time."""
    if batch_size is None:
      batch_size = db.CLIENT_IDS_BATCH_SIZE

    while True:
      deleted_count = self._DeleteClientStatsBatch(cutoff_time, batch_size)

      # Do not yield a trailing 0 which occurs when an exact multiple of
      # `yield_after_count` rows were in the table.
      if deleted_count > 0:
        yield deleted_count
      else:
        break

  @mysql_utils.WithTransaction()
  def _DeleteClientStatsBatch(
      self,
      cutoff_time: rdfvalue.RDFDatetime,
      batch_size: int,
      cursor: Optional[MySQLdb.cursors.Cursor] = None,
  ) -> int:
    """Deletes up to `limit` ClientStats older than `retention_time`."""
    cursor.execute(
        "DELETE FROM client_stats WHERE timestamp < FROM_UNIXTIME(%s) LIMIT %s",
        [mysql_utils.RDFDatetimeToTimestamp(cutoff_time), batch_size])
    return cursor.rowcount

  @mysql_utils.WithTransaction(readonly=True)
  def CountClientVersionStringsByLabel(self, day_buckets, cursor):
    """Computes client-activity stats for all GRR versions in the DB."""
    return self._CountClientStatisticByLabel("last_version_string", day_buckets,
                                             cursor)

  @mysql_utils.WithTransaction(readonly=True)
  def CountClientPlatformsByLabel(self, day_buckets, cursor):
    """Computes client-activity stats for all client platforms in the DB."""
    return self._CountClientStatisticByLabel("last_platform", day_buckets,
                                             cursor)

  @mysql_utils.WithTransaction(readonly=True)
  def CountClientPlatformReleasesByLabel(self, day_buckets, cursor):
    """Computes client-activity stats for OS-release strings in the DB."""
    return self._CountClientStatisticByLabel("last_platform_release",
                                             day_buckets, cursor)

  def _CountClientStatisticByLabel(self, statistic, day_buckets, cursor):
    """Returns client-activity metrics for a given statistic.

    Args:
      statistic: The name of the statistic, which should also be a column in the
        'clients' table.
      day_buckets: A set of n-day-active buckets.
      cursor: MySQL cursor for executing queries.
    """
    day_buckets = sorted(day_buckets)
    sum_clauses = []
    ping_cast_clauses = []
    timestamp_buckets = []
    now = rdfvalue.RDFDatetime.Now()

    for day_bucket in day_buckets:
      column_name = "days_active_{}".format(day_bucket)
      sum_clauses.append(
          "CAST(SUM({0}) AS UNSIGNED) AS {0}".format(column_name))
      ping_cast_clauses.append(
          "CAST(c.last_ping > FROM_UNIXTIME(%s) AS UNSIGNED) AS {}".format(
              column_name))
      timestamp_bucket = now - rdfvalue.Duration.From(day_bucket, rdfvalue.DAYS)
      timestamp_buckets.append(
          mysql_utils.RDFDatetimeToTimestamp(timestamp_bucket))

    # Count all clients with a label owned by 'GRR', aggregating by label.
    query = """
    SELECT j.{statistic}, j.label, {sum_clauses}
    FROM (
      SELECT c.{statistic} AS {statistic}, l.label AS label, {ping_cast_clauses}
      FROM clients c
      LEFT JOIN client_labels l USING(client_id)
      WHERE c.last_ping IS NOT NULL AND l.owner_username = 'GRR'
    ) AS j
    GROUP BY j.{statistic}, j.label
    """.format(
        statistic=statistic,
        sum_clauses=", ".join(sum_clauses),
        ping_cast_clauses=", ".join(ping_cast_clauses))

    cursor.execute(query, timestamp_buckets)

    fleet_stats_builder = fleet_utils.FleetStatsBuilder(day_buckets)
    for response_row in cursor.fetchall():
      statistic_value, client_label = response_row[:2]
      for i, num_actives in enumerate(response_row[2:]):
        if num_actives <= 0:
          continue
        fleet_stats_builder.IncrementLabel(
            client_label, statistic_value, day_buckets[i], delta=num_actives)

    # Get n-day-active totals for the statistic across all clients (including
    # those that do not have a 'GRR' label).
    query = """
    SELECT j.{statistic}, {sum_clauses}
    FROM (
      SELECT c.{statistic} AS {statistic}, {ping_cast_clauses}
      FROM clients c
      WHERE c.last_ping IS NOT NULL
    ) AS j
    GROUP BY j.{statistic}
    """.format(
        statistic=statistic,
        sum_clauses=", ".join(sum_clauses),
        ping_cast_clauses=", ".join(ping_cast_clauses))

    cursor.execute(query, timestamp_buckets)

    for response_row in cursor.fetchall():
      statistic_value = response_row[0]
      for i, num_actives in enumerate(response_row[1:]):
        if num_actives <= 0:
          continue
        fleet_stats_builder.IncrementTotal(
            statistic_value, day_buckets[i], delta=num_actives)

    return fleet_stats_builder.Build()

  @mysql_utils.WithTransaction()
  def DeleteClient(self, client_id, cursor=None):
    """Deletes a client with all associated metadata."""
    cursor.execute("SELECT COUNT(*) FROM clients WHERE client_id = %s",
                   [db_utils.ClientIDToInt(client_id)])

    if cursor.fetchone()[0] == 0:
      raise db.UnknownClientError(client_id)

    # Clean out foreign keys first.
    cursor.execute(
        """
    UPDATE clients SET
      last_crash_timestamp = NULL,
      last_snapshot_timestamp = NULL,
      last_startup_timestamp = NULL
    WHERE client_id = %s""", [db_utils.ClientIDToInt(client_id)])

    cursor.execute("DELETE FROM clients WHERE client_id = %s",
                   [db_utils.ClientIDToInt(client_id)])

  def StructuredSearchClients(self, expression: rdf_search.SearchExpression,
                              sort_order: rdf_search.SortOrder,
                              continuation_token: bytes,
                              number_of_results: int) -> db.SearchClientsResult:
    # Unused arguments
    del self, expression, sort_order, continuation_token, number_of_results
    raise NotImplementedError


# We use the same value as other database implementations that we have some
# measures for. However, MySQL has different performance characteristics and it
# could be fine-tuned if possible.
_DEFAULT_CLIENT_STATS_BATCH_SIZE = 10_000
