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

import org.apache.commons.io.FileUtils;
import org.apache.commons.logging.Log;
import org.apache.commons.logging.LogFactory;
import org.apache.hadoop.classification.InterfaceAudience;
import org.apache.hadoop.classification.InterfaceStability;
import org.apache.hadoop.conf.Configuration;
import org.codehaus.jackson.annotate.JsonCreator;
import org.codehaus.jackson.annotate.JsonProperty;
import org.codehaus.jackson.annotate.JsonSubTypes;
import org.codehaus.jackson.map.ObjectMapper;

import java.io.File;
import java.io.IOException;
import java.util.Date;

import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.TimelineMetricConfiguration.AGGREGATOR_CHECKPOINT_DELAY;
import static org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline.TimelineMetricConfiguration.RESULTSET_FETCH_SIZE;

public abstract class AbstractTimelineAggregator implements Runnable {
  protected final PhoenixHBaseAccessor hBaseAccessor;
  private final Log LOG;
  private static final ObjectMapper mapper;
  protected final long checkpointDelay;
  protected final Integer resultsetFetchSize;
  protected Configuration metricsConf;

  static {
    mapper = new ObjectMapper();
  }

  public AbstractTimelineAggregator(PhoenixHBaseAccessor hBaseAccessor,
                                    Configuration metricsConf) {
    this.hBaseAccessor = hBaseAccessor;
    this.metricsConf = metricsConf;
    this.checkpointDelay = metricsConf.getInt(AGGREGATOR_CHECKPOINT_DELAY, 120000);
    this.resultsetFetchSize = metricsConf.getInt(RESULTSET_FETCH_SIZE, 2000);
    this.LOG = LogFactory.getLog(this.getClass());
  }

  @Override
  public void run() {
    LOG.info("Started Timeline aggregator thread @ " + new Date());
    Long SLEEP_INTERVAL = getSleepInterval();

    while (true) {
      long currentTime = System.currentTimeMillis();
      long lastCheckPointTime = -1;

      try {
        lastCheckPointTime = readCheckPoint();
        if (isLastCheckPointTooOld(lastCheckPointTime)) {
          LOG.warn("Last Checkpoint is too old, discarding last checkpoint. " +
            "lastCheckPointTime = " + lastCheckPointTime);
          lastCheckPointTime = -1;
        }
        if (lastCheckPointTime == -1) {
          // Assuming first run, save checkpoint and sleep.
          // Set checkpoint to 2 minutes in the past to allow the
          // agents/collectors to catch up
          saveCheckPoint(currentTime - checkpointDelay);
        }
      } catch (IOException io) {
        LOG.warn("Unable to write last checkpoint time. Resuming sleep.", io);
      }
      long sleepTime = SLEEP_INTERVAL;

      if (lastCheckPointTime != -1) {
        LOG.info("Last check point time: " + lastCheckPointTime + ", lagBy: "
          + ((System.currentTimeMillis() - lastCheckPointTime) / 1000)
          + " seconds." );

        long startTime = System.currentTimeMillis();
        boolean success = doWork(lastCheckPointTime, lastCheckPointTime + SLEEP_INTERVAL);
        long executionTime = System.currentTimeMillis() - startTime;
        long delta = SLEEP_INTERVAL - executionTime;

        if (delta > 0) {
          // Sleep for (configured sleep - time to execute task)
          sleepTime = delta;
        } else {
          // No sleep because last run took too long to execute
          LOG.info("Aggregator execution took too long, " +
            "cancelling sleep. executionTime = " + executionTime);
          sleepTime = 1;
        }

        LOG.debug("Aggregator sleep interval = " + sleepTime);

        if (success) {
          try {
            saveCheckPoint(lastCheckPointTime + SLEEP_INTERVAL);
          } catch (IOException io) {
            LOG.warn("Error saving checkpoint, restarting aggregation at " +
              "previous checkpoint.");
          }
        }
      }

      try {
        Thread.sleep(sleepTime);
      } catch (InterruptedException e) {
        LOG.info("Sleep interrupted, continuing with aggregation.");
      }
    }
  }

  private boolean isLastCheckPointTooOld(long checkpoint) {
    return checkpoint != -1 &&
      ((System.currentTimeMillis() - checkpoint) > getCheckpointCutOffInterval());
  }

  private long readCheckPoint() {
    try {
      File checkpoint = new File(getCheckpointLocation());
      if (checkpoint.exists()) {
        String contents = FileUtils.readFileToString(checkpoint);
        if (contents != null && !contents.isEmpty()) {
          return Long.parseLong(contents);
        }
      }
    } catch (IOException io) {
      LOG.debug(io);
    }
    return -1;
  }

  private void saveCheckPoint(long checkpointTime) throws IOException {
    File checkpoint = new File(getCheckpointLocation());
    if (!checkpoint.exists()) {
      boolean done = checkpoint.createNewFile();
      if (!done) {
        throw new IOException("Could not create checkpoint at location, " +
          getCheckpointLocation());
      }
    }
    FileUtils.writeStringToFile(checkpoint, String.valueOf(checkpointTime));
  }

  // TODO: Abstract out doWork implementation for cluster and host levels
  protected abstract boolean doWork(long startTime, long endTime);

  protected abstract Long getSleepInterval();

  protected abstract Integer getCheckpointCutOffMultiplier();

  protected Long getCheckpointCutOffInterval() {
    return getCheckpointCutOffMultiplier() * getSleepInterval();
  }

  protected abstract String getCheckpointLocation();

  @JsonSubTypes({ @JsonSubTypes.Type(value = MetricClusterAggregate.class),
                @JsonSubTypes.Type(value = MetricHostAggregate.class) })
  @InterfaceAudience.Public
  @InterfaceStability.Unstable
  public static class MetricAggregate {
    protected Double sum = 0.0;
    protected Double deviation;
    protected Double max = Double.MIN_VALUE;
    protected Double min = Double.MAX_VALUE;

    public MetricAggregate() {}

    protected MetricAggregate(Double sum, Double deviation, Double max, Double min) {
      this.sum = sum;
      this.deviation = deviation;
      this.max = max;
      this.min = min;
    }

    void updateSum(Double sum) {
      this.sum += sum;
    }

    void updateMax(Double max) {
      if (max > this.max) {
        this.max = max;
      }
    }

    void updateMin(Double min) {
      if (min < this.min) {
        this.min = min;
      }
    }

    @JsonProperty("sum")
    Double getSum() {
      return sum;
    }

    @JsonProperty("deviation")
    Double getDeviation() {
      return deviation;
    }

    @JsonProperty("max")
    Double getMax() {
      return max;
    }

    @JsonProperty("min")
    Double getMin() {
      return min;
    }

    public void setSum(Double sum) {
      this.sum = sum;
    }

    public void setDeviation(Double deviation) {
      this.deviation = deviation;
    }

    public void setMax(Double max) {
      this.max = max;
    }

    public void setMin(Double min) {
      this.min = min;
    }

    public String toJSON() throws IOException {
      return mapper.writeValueAsString(this);
    }
  }

  public static class MetricClusterAggregate extends MetricAggregate {
    private int numberOfHosts;

    @JsonCreator
    public MetricClusterAggregate() {}

    MetricClusterAggregate(Double sum, int numberOfHosts, Double deviation,
                           Double max, Double min) {
      super(sum, deviation, max, min);
      this.numberOfHosts = numberOfHosts;
    }

    @JsonProperty("numberOfHosts")
    int getNumberOfHosts() {
      return numberOfHosts;
    }

    void updateNumberOfHosts(int count) {
      this.numberOfHosts += count;
    }

    public void setNumberOfHosts(int numberOfHosts) {
      this.numberOfHosts = numberOfHosts;
    }

    @Override
    public String toString() {
      return "MetricAggregate{" +
        "sum=" + sum +
        ", numberOfHosts=" + numberOfHosts +
        ", deviation=" + deviation +
        ", max=" + max +
        ", min=" + min +
        '}';
    }
  }

  /**
   * Represents a collection of minute based aggregation of values for
   * resolution greater than a minute.
   */
  public static class MetricHostAggregate extends MetricAggregate {

    @JsonCreator
    public MetricHostAggregate() {
      super(0.0, 0.0, Double.MIN_VALUE, Double.MAX_VALUE);
    }

    /**
     * Find and update min, max and avg for a minute
     */
    void updateAggregates(MetricHostAggregate hostAggregate) {
      updateMax(hostAggregate.getMax());
      updateMin(hostAggregate.getMin());
      updateSum(hostAggregate.getSum());
    }

    /**
     * Reuse sum to indicate average for a host for the hour
     */
    @Override
    void updateSum(Double sum) {
      this.sum = (this.sum + sum) / 2;
    }

    @Override
    public String toString() {
      return "MetricHostAggregate{" +
        "sum=" + sum +
        ", deviation=" + deviation +
        ", max=" + max +
        ", min=" + min +
        '}';
    }
  }
}
