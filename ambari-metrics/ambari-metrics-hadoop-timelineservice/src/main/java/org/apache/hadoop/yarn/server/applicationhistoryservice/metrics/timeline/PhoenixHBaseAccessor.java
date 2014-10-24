/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline;

import org.apache.commons.logging.Log;
import org.apache.commons.logging.LogFactory;
import org.apache.hadoop.conf.Configuration;
import org.apache.hadoop.metrics2.sink.timeline.TimelineMetric;
import org.apache.hadoop.metrics2.sink.timeline.TimelineMetrics;
import org.apache.hadoop.yarn.util.timeline.TimelineUtils;
import org.codehaus.jackson.map.ObjectMapper;
import org.codehaus.jackson.type.TypeReference;

import java.io.IOException;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.AbstractTimelineAggregator.MetricClusterAggregate;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.AbstractTimelineAggregator.MetricHostAggregate;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.PhoenixTransactSQL.CREATE_METRICS_AGGREGATE_HOURLY_TABLE_SQL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.PhoenixTransactSQL.CREATE_METRICS_AGGREGATE_MINUTE_TABLE_SQL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.PhoenixTransactSQL.CREATE_METRICS_CLUSTER_AGGREGATE_HOURLY_TABLE_SQL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.PhoenixTransactSQL.CREATE_METRICS_CLUSTER_AGGREGATE_TABLE_SQL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.PhoenixTransactSQL.CREATE_METRICS_TABLE_SQL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.PhoenixTransactSQL.Condition;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.PhoenixTransactSQL.DEFAULT_ENCODING;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.PhoenixTransactSQL.DEFAULT_TABLE_COMPRESSION;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.PhoenixTransactSQL.METRICS_RECORD_TABLE_NAME;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.PhoenixTransactSQL.UPSERT_AGGREGATE_RECORD_SQL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.PhoenixTransactSQL.UPSERT_CLUSTER_AGGREGATE_SQL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.PhoenixTransactSQL.UPSERT_METRICS_SQL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.TimelineMetricClusterAggregator.TimelineClusterMetric;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.TimelineMetricConfiguration.CLUSTER_HOUR_TABLE_TTL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.TimelineMetricConfiguration.CLUSTER_MINUTE_TABLE_TTL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.TimelineMetricConfiguration.HBASE_COMPRESSION_SCHEME;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.TimelineMetricConfiguration.HBASE_ENCODING_SCHEME;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.TimelineMetricConfiguration.HOST_HOUR_TABLE_TTL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.TimelineMetricConfiguration.HOST_MINUTE_TABLE_TTL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.TimelineMetricConfiguration.PRECISION_TABLE_TTL;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.TimelineMetricConfiguration.RESULTSET_FETCH_SIZE;

/**
 * Provides a facade over the Phoenix API to access HBase schema
 */
public class PhoenixHBaseAccessor {

  private final Configuration hbaseConf;
  private final Configuration metricsConf;
  static final Log LOG = LogFactory.getLog(PhoenixHBaseAccessor.class);
  private static final String connectionUrl = "jdbc:phoenix:%s:%s:%s";
  private static final String ZOOKEEPER_CLIENT_PORT =
    "hbase.zookeeper.property.clientPort";
  private static final String ZOOKEEPER_QUORUM = "hbase.zookeeper.quorum";
  private static final String ZNODE_PARENT = "zookeeper.znode.parent";
  static final int PHOENIX_MAX_MUTATION_STATE_SIZE = 50000;
  /**
   * 4 metrics/min * 60 * 24: Retrieve data for 1 day.
   */
  public static int RESULTSET_LIMIT = 5760;
  private static ObjectMapper mapper;

  static {
    mapper = new ObjectMapper();
  }

  private static TypeReference<Map<Long, Double>> metricValuesTypeRef =
    new TypeReference<Map<Long, Double>>() {};

  public PhoenixHBaseAccessor(Configuration hbaseConf, Configuration metricsConf) {
    this.hbaseConf = hbaseConf;
    this.metricsConf = metricsConf;
    RESULTSET_LIMIT = metricsConf.getInt(RESULTSET_FETCH_SIZE, 5760);
    try {
      Class.forName("org.apache.phoenix.jdbc.PhoenixDriver");
    } catch (ClassNotFoundException e) {
      LOG.error("Phoenix client jar not found in the classpath.", e);
      throw new IllegalStateException(e);
    }
  }

  /**
   * Get JDBC connection to HBase store. Assumption is that the hbase
   * configuration is present on the classpath and loaded by the caller into
   * the Configuration object.
   * Phoenix already caches the HConnection between the client and HBase
   * cluster.
   * @return @java.sql.Connection
   */
  protected Connection getConnection() {
    Connection connection = null;
    String zookeeperClientPort = hbaseConf.getTrimmed(ZOOKEEPER_CLIENT_PORT, "2181");
    String zookeeperQuorum = hbaseConf.getTrimmed(ZOOKEEPER_QUORUM);
    String znodeParent = hbaseConf.getTrimmed(ZNODE_PARENT, "/hbase");

    if (zookeeperQuorum == null || zookeeperQuorum.isEmpty()) {
      throw new IllegalStateException("Unable to find Zookeeper quorum to " +
        "access HBase store using Phoenix.");
    }

    String url = String.format(connectionUrl, zookeeperQuorum,
      zookeeperClientPort, znodeParent);

    LOG.debug("Metric store connection url: " + url);

    try {
      connection = DriverManager.getConnection(url);
    } catch (SQLException e) {
      LOG.warn("Unable to connect to HBase store using Phoenix.", e);
    }

    return connection;
  }

  public static Map readMetricFromJSON(String json) throws IOException {
    return mapper.readValue(json, metricValuesTypeRef);
  }

  @SuppressWarnings("unchecked")
  static TimelineMetric getTimelineMetricFromResultSet(ResultSet rs)
      throws SQLException, IOException {
    TimelineMetric metric = new TimelineMetric();
    metric.setMetricName(rs.getString("METRIC_NAME"));
    metric.setAppId(rs.getString("APP_ID"));
    metric.setInstanceId(rs.getString("INSTANCE_ID"));
    metric.setHostName(rs.getString("HOSTNAME"));
    metric.setTimestamp(rs.getLong("SERVER_TIME"));
    metric.setStartTime(rs.getLong("START_TIME"));
    metric.setType(rs.getString("UNITS"));
    metric.setMetricValues(
      (Map<Long, Double>) readMetricFromJSON(rs.getString("METRICS")));
    return metric;
  }

  static TimelineMetric getTimelineMetricKeyFromResultSet(ResultSet rs)
      throws SQLException, IOException {
    TimelineMetric metric = new TimelineMetric();
    metric.setMetricName(rs.getString("METRIC_NAME"));
    metric.setAppId(rs.getString("APP_ID"));
    metric.setInstanceId(rs.getString("INSTANCE_ID"));
    metric.setHostName(rs.getString("HOSTNAME"));
    metric.setTimestamp(rs.getLong("SERVER_TIME"));
    metric.setType(rs.getString("UNITS"));
    return metric;
  }

  static MetricHostAggregate getMetricHostAggregateFromResultSet(ResultSet rs)
      throws SQLException {
    MetricHostAggregate metricHostAggregate = new MetricHostAggregate();
    metricHostAggregate.setSum(rs.getDouble("METRIC_AVG"));
    metricHostAggregate.setMax(rs.getDouble("METRIC_MAX"));
    metricHostAggregate.setMin(rs.getDouble("METRIC_MIN"));
    metricHostAggregate.setDeviation(0.0);
    return metricHostAggregate;
  }


  protected void initMetricSchema() {
    Connection conn = getConnection();
    Statement stmt = null;

    String encoding = metricsConf.get(HBASE_ENCODING_SCHEME, DEFAULT_ENCODING);
    String compression = metricsConf.get(HBASE_COMPRESSION_SCHEME, DEFAULT_TABLE_COMPRESSION);
    String precisionTtl = metricsConf.get(PRECISION_TABLE_TTL, "86400");
    String hostMinTtl = metricsConf.get(HOST_MINUTE_TABLE_TTL, "604800");
    String hostHourTtl = metricsConf.get(HOST_HOUR_TABLE_TTL, "2592000");
    String clusterMinTtl = metricsConf.get(CLUSTER_MINUTE_TABLE_TTL, "2592000");
    String clusterHourTtl = metricsConf.get(CLUSTER_HOUR_TABLE_TTL, "31536000");

    try {
      LOG.info("Initializing metrics schema...");
      stmt = conn.createStatement();
      stmt.executeUpdate(String.format(CREATE_METRICS_TABLE_SQL,
        encoding, precisionTtl, compression));
      stmt.executeUpdate(String.format(CREATE_METRICS_AGGREGATE_HOURLY_TABLE_SQL,
        encoding, hostHourTtl, compression));
      stmt.executeUpdate(String.format(CREATE_METRICS_AGGREGATE_MINUTE_TABLE_SQL,
        encoding, hostMinTtl, compression));
      stmt.executeUpdate(String.format(CREATE_METRICS_CLUSTER_AGGREGATE_TABLE_SQL,
        encoding, clusterMinTtl, compression));
      stmt.executeUpdate(String.format(CREATE_METRICS_CLUSTER_AGGREGATE_HOURLY_TABLE_SQL,
          encoding, clusterHourTtl, compression));
      conn.commit();
    } catch (SQLException sql) {
      LOG.warn("Error creating Metrics Schema in HBase using Phoenix.", sql);
    } finally {
      if (stmt != null) {
        try {
          stmt.close();
        } catch (SQLException e) {
          // Ignore
        }
      }
      if (conn != null) {
        try {
          conn.close();
        } catch (SQLException e) {
          // Ignore
        }
      }
    }
  }

  public void insertMetricRecords(TimelineMetrics metrics)
      throws SQLException, IOException {

    List<TimelineMetric> timelineMetrics = metrics.getMetrics();
    if (timelineMetrics == null || timelineMetrics.isEmpty()) {
      LOG.debug("Empty metrics insert request.");
      return;
    }

    Connection conn = getConnection();
    PreparedStatement metricRecordStmt = null;
    PreparedStatement metricRecordTmpStmt = null;
    long currentTime = System.currentTimeMillis();

    try {
      metricRecordStmt = conn.prepareStatement(String.format(
        UPSERT_METRICS_SQL, METRICS_RECORD_TABLE_NAME));
      /*metricRecordTmpStmt = conn.prepareStatement(String.format
        (UPSERT_METRICS_SQL, METRICS_RECORD_CACHE_TABLE_NAME));*/

      for (TimelineMetric metric : timelineMetrics) {
        metricRecordStmt.clearParameters();

        LOG.trace("host: " + metric.getHostName() + ", " +
          "metricName = " + metric.getMetricName() + ", " +
          "values: " + metric.getMetricValues());
        Double[] aggregates = calculateAggregates(metric.getMetricValues());

        metricRecordStmt.setString(1, metric.getMetricName());
        //metricRecordTmpStmt.setString(1, metric.getMetricName());
        metricRecordStmt.setString(2, metric.getHostName());
        //metricRecordTmpStmt.setString(2, metric.getHostName());
        metricRecordStmt.setString(3, metric.getAppId());
        //metricRecordTmpStmt.setString(3, metric.getAppId());
        metricRecordStmt.setString(4, metric.getInstanceId());
        //metricRecordTmpStmt.setString(4, metric.getInstanceId());
        metricRecordStmt.setLong(5, currentTime);
        //metricRecordTmpStmt.setLong(5, currentTime);
        metricRecordStmt.setLong(6, metric.getStartTime());
        //metricRecordTmpStmt.setLong(6, metric.getStartTime());
        metricRecordStmt.setString(7, metric.getType());
        //metricRecordTmpStmt.setString(7, metric.getType());
        metricRecordStmt.setDouble(8, aggregates[0]);
        //metricRecordTmpStmt.setDouble(8, aggregates[0]);
        metricRecordStmt.setDouble(9, aggregates[1]);
        //metricRecordTmpStmt.setDouble(9, aggregates[1]);
        metricRecordStmt.setDouble(10, aggregates[2]);
        //metricRecordTmpStmt.setDouble(10, aggregates[2]);
        String json =
          TimelineUtils.dumpTimelineRecordtoJSON(metric.getMetricValues());
        metricRecordStmt.setString(11, json);
        //metricRecordTmpStmt.setString(11, json);

        try {
          metricRecordStmt.executeUpdate();
          //metricRecordTmpStmt.executeUpdate();
        } catch (SQLException sql) {
          LOG.error(sql);
        }
      }

      conn.commit();

    } finally {
      if (metricRecordStmt != null) {
        try {
          metricRecordStmt.close();
        } catch (SQLException e) {
          // Ignore
        }
      }
      if (metricRecordTmpStmt != null) {
        try {
          metricRecordTmpStmt.close();
        } catch (SQLException e) {
          // Ignore
        }
      }
      if (conn != null) {
        try {
          conn.close();
        } catch (SQLException sql) {
          // Ignore
        }
      }
    }
  }

  private Double[] calculateAggregates(Map<Long, Double> metricValues) {
    Double[] values = new Double[3];
    Double max = Double.MIN_VALUE;
    Double min = Double.MAX_VALUE;
    Double avg = 0.0;
    if (metricValues != null && !metricValues.isEmpty()) {
      for (Double value : metricValues.values()) {
        // TODO: Some nulls in data - need to investigate null values from host
        if (value != null) {
          if (value > max) {
            max  = value;
          }
          if (value < min) {
            min = value;
          }
          avg += value;
        }
      }
      avg /= metricValues.values().size();
    }
    values[0] = max != Double.MIN_VALUE ? max : 0.0;
    values[1] = min != Double.MAX_VALUE ? min : 0.0;
    values[2] = avg;
    return values;
  }

  @SuppressWarnings("unchecked")
  public TimelineMetrics getMetricRecords(final Condition condition)
      throws SQLException, IOException {

    if (condition.isEmpty()) {
      throw new SQLException("No filter criteria specified.");
    }

    Connection conn = getConnection();
    PreparedStatement stmt = null;
    TimelineMetrics metrics = new TimelineMetrics();

    try {
      stmt = PhoenixTransactSQL.prepareGetMetricsSqlStmt(conn, condition);

      ResultSet rs = stmt.executeQuery();

      while (rs.next()) {
        TimelineMetric metric = getTimelineMetricFromResultSet(rs);

        if (condition.isGrouped()) {
          metrics.addOrMergeTimelineMetric(metric);
        } else {
          metrics.getMetrics().add(metric);
        }
      }

    } finally {
      if (stmt != null) {
        try {
          stmt.close();
        } catch (SQLException e) {
          // Ignore
        }
      }
      if (conn != null) {
        try {
          conn.close();
        } catch (SQLException sql) {
          // Ignore
        }
      }
    }
    return metrics;
  }

  public void saveHostAggregateRecords(Map<TimelineMetric,
      MetricHostAggregate> hostAggregateMap, String phoenixTableName)
      throws SQLException {

    if (hostAggregateMap != null && !hostAggregateMap.isEmpty()) {
      Connection conn = getConnection();
      PreparedStatement stmt = null;

      long start = System.currentTimeMillis();
      int rowCount = 0;

      try {
        stmt = conn.prepareStatement(
          String.format(UPSERT_AGGREGATE_RECORD_SQL, phoenixTableName));

        for (Map.Entry<TimelineMetric, MetricHostAggregate> metricAggregate :
            hostAggregateMap.entrySet()) {

          TimelineMetric metric = metricAggregate.getKey();
          MetricHostAggregate hostAggregate = metricAggregate.getValue();

          rowCount++;
          stmt.clearParameters();
          stmt.setString(1, metric.getMetricName());
          stmt.setString(2, metric.getHostName());
          stmt.setString(3, metric.getAppId());
          stmt.setString(4, metric.getInstanceId());
          stmt.setLong(5, metric.getTimestamp());
          stmt.setString(6, metric.getType());
          stmt.setDouble(7, hostAggregate.getSum());
          stmt.setDouble(8, hostAggregate.getMax());
          stmt.setDouble(9, hostAggregate.getMin());

          try {
            stmt.executeUpdate();
          } catch (SQLException sql) {
            LOG.error(sql);
          }

          if (rowCount >= PHOENIX_MAX_MUTATION_STATE_SIZE - 1) {
            conn.commit();
            rowCount = 0;
          }

        }

        conn.commit();

      } finally {
        if (stmt != null) {
          try {
            stmt.close();
          } catch (SQLException e) {
            // Ignore
          }
        }
        if (conn != null) {
          try {
            conn.close();
          } catch (SQLException sql) {
            // Ignore
          }
        }
      }

      long end = System.currentTimeMillis();

      if ((end - start) > 60000l) {
        LOG.info("Time to save map: " + (end - start) + ", " +
          "thread = " + Thread.currentThread().getClass());
      }
    }
  }

  /**
   * Save Metric aggregate records.
   * @throws SQLException
   */
  public void saveClusterAggregateRecords(Map<TimelineClusterMetric,
      MetricClusterAggregate> records) throws SQLException {
    if (records == null || records.isEmpty()) {
      LOG.debug("Empty aggregate records.");
      return;
    }

    long start = System.currentTimeMillis();

    Connection conn = getConnection();
    PreparedStatement stmt = null;
    try {
      stmt = conn.prepareStatement(UPSERT_CLUSTER_AGGREGATE_SQL);
      int rowCount = 0;

      for (Map.Entry<TimelineClusterMetric, MetricClusterAggregate>
          aggregateEntry : records.entrySet()) {
        TimelineClusterMetric clusterMetric = aggregateEntry.getKey();
        MetricClusterAggregate aggregate = aggregateEntry.getValue();

        LOG.trace("clusterMetric = " + clusterMetric + ", " +
          "aggregate = " + aggregate);

        rowCount++;
        stmt.clearParameters();
        stmt.setString(1, clusterMetric.getMetricName());
        stmt.setString(2, clusterMetric.getAppId());
        stmt.setString(3, clusterMetric.getInstanceId());
        stmt.setLong(4, clusterMetric.getTimestamp());
        stmt.setString(5, clusterMetric.getType());
        stmt.setDouble(6, aggregate.getSum());
        stmt.setInt(7, aggregate.getNumberOfHosts());
        stmt.setDouble(8, aggregate.getMax());
        stmt.setDouble(9, aggregate.getMin());

        try {
          stmt.executeUpdate();
        } catch (SQLException sql) {
          LOG.error(sql);
        }

        if (rowCount >= PHOENIX_MAX_MUTATION_STATE_SIZE - 1) {
          conn.commit();
          rowCount = 0;
        }
      }

      conn.commit();

    } finally {
      if (stmt != null) {
        try {
          stmt.close();
        } catch (SQLException e) {
          // Ignore
        }
      }
      if (conn != null) {
        try {
          conn.close();
        } catch (SQLException sql) {
          // Ignore
        }
      }
    }
    long end = System.currentTimeMillis();
    if ((end - start) > 60000l) {
      LOG.info("Time to save: " + (end - start) + ", " +
        "thread = " + Thread.currentThread().getName());
    }
  }


  public TimelineMetrics getAggregateMetricRecords(final Condition condition)
      throws SQLException {

    if (condition.isEmpty()) {
      throw new SQLException("No filter criteria specified.");
    }

    Connection conn = getConnection();
    PreparedStatement stmt = null;
    TimelineMetrics metrics = new TimelineMetrics();

    try {
      stmt = PhoenixTransactSQL.prepareGetAggregateSqlStmt(conn, condition);

      ResultSet rs = stmt.executeQuery();

      while (rs.next()) {
        TimelineMetric metric = new TimelineMetric();
        metric.setMetricName(rs.getString("METRIC_NAME"));
        metric.setAppId(rs.getString("APP_ID"));
        metric.setInstanceId(rs.getString("INSTANCE_ID"));
        metric.setTimestamp(rs.getLong("SERVER_TIME"));
        metric.setStartTime(rs.getLong("SERVER_TIME"));
        Map<Long, Double> valueMap = new HashMap<Long, Double>();
        valueMap.put(rs.getLong("SERVER_TIME"), rs.getDouble("METRIC_SUM") /
                                              rs.getInt("HOSTS_COUNT"));
        metric.setMetricValues(valueMap);

        if (condition.isGrouped()) {
          metrics.addOrMergeTimelineMetric(metric);
        } else {
          metrics.getMetrics().add(metric);
        }
      }

    } finally {
      if (stmt != null) {
        try {
          stmt.close();
        } catch (SQLException e) {
          // Ignore
        }
      }
      if (conn != null) {
        try {
          conn.close();
        } catch (SQLException sql) {
          // Ignore
        }
      }
    }
    LOG.info("Aggregate records size: " + metrics.getMetrics().size());
    return metrics;
  }
}
