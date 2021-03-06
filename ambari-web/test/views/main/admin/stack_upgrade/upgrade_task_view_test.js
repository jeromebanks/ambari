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


var App = require('app');
require('views/main/admin/stack_upgrade/upgrade_task_view');

describe('App.upgradeTaskView', function () {
  var view = App.upgradeTaskView.create({
    content: Em.Object.create(),
    taskDetailsProperties: ['prop1']
  });
  view.removeObserver('content.isExpanded', view, 'doPolling');

  describe("#logTabId", function() {
    it("", function() {
      view.reopen({
        elementId: 'elementId'
      });
      expect(view.get('logTabId')).to.equal('elementId-log-tab');
    });
  });

  describe("#errorTabId", function() {
    it("", function() {
      view.reopen({
        elementId: 'elementId'
      });
      expect(view.get('errorTabId')).to.equal('elementId-error-tab');
    });
  });

  describe("#logTabIdLink", function() {
    it("", function() {
      view.reopen({
        logTabId: 'elementId-log-tab'
      });
      expect(view.get('logTabIdLink')).to.equal('#elementId-log-tab');
    });
  });

  describe("#errorTabIdLInk", function() {
    it("", function() {
      view.reopen({
        errorTabId: 'elementId-error-tab'
      });
      expect(view.get('errorTabIdLInk')).to.equal('#elementId-error-tab');
    });
  });

  describe("#doPolling()", function () {
    beforeEach(function () {
      sinon.stub(view, 'getTaskDetails', Em.K);
      sinon.spy(view, 'doPolling');
      this.clock = sinon.useFakeTimers();
    });
    afterEach(function () {
      view.getTaskDetails.restore();
      view.doPolling.restore();
      this.clock.restore();
    });
    it("isExpanded false", function () {
      view.set('content.isExpanded', false);
      view.doPolling();
      expect(view.getTaskDetails.called).to.be.false;
    });
    it("isExpanded true", function () {
      view.set('content.isExpanded', true);
      view.doPolling();
      expect(view.getTaskDetails.calledOnce).to.be.true;
      this.clock.tick(App.bgOperationsUpdateInterval);
      expect(view.doPolling.calledTwice).to.be.true;
    });
  });

  describe("#getTaskDetails()", function () {
    beforeEach(function () {
      sinon.stub(App.ajax, 'send', Em.K);

    });
    afterEach(function () {
      App.ajax.send.restore();
    });
    it("call App.ajax.send()", function () {
      view.set('content.id', 1);
      view.set('content.request_id', 1);
      view.getTaskDetails();
      expect(App.ajax.send.getCall(0).args[0]).to.eql({
        name: 'admin.upgrade.task',
        sender: view,
        data: {
          upgradeId: 1,
          taskId: 1
        },
        success: 'getTaskDetailsSuccessCallback'
      });
    });
  });

  describe("#getTaskDetailsSuccessCallback()", function () {
    it("", function () {
      var data = {
        items: [
          {
            upgrade_items: [
              {
                tasks: [
                  {
                    Tasks: {
                      prop1: 'value'
                    }
                  }
                ]
              }
            ]
          }
        ]
      };
      view.getTaskDetailsSuccessCallback(data);
      expect(view.get('content.prop1')).to.equal('value');
    });
  });

  describe("#copyErrLog()", function () {
    before(function () {
      sinon.stub(view, 'toggleProperty', Em.K);
    });
    after(function () {
      view.toggleProperty.restore();
    });
    it("", function () {
      view.copyErrLog();
      expect(view.toggleProperty.calledWith('errorLogOpened')).to.be.true;
    });
  });

  describe("#copyOutLog()", function () {
    before(function () {
      sinon.stub(view, 'toggleProperty', Em.K);
    });
    after(function () {
      view.toggleProperty.restore();
    });
    it("", function () {
      view.copyOutLog();
      expect(view.toggleProperty.calledWith('outputLogOpened')).to.be.true;
    });
  });

  describe("#openErrorLog()", function () {
    before(function () {
      sinon.stub(view, 'openLogWindow', Em.K);
    });
    after(function () {
      view.openLogWindow.restore();
    });
    it("", function () {
      view.set('content.stderr', 'stderr');
      view.openErrorLog();
      expect(view.openLogWindow.calledWith('stderr')).to.be.true;
    });
  });

  describe("#openOutLog()", function () {
    before(function () {
      sinon.stub(view, 'openLogWindow', Em.K);
    });
    after(function () {
      view.openLogWindow.restore();
    });
    it("", function () {
      view.set('content.stdout', 'stdout');
      view.openOutLog();
      expect(view.openLogWindow.calledWith('stdout')).to.be.true;
    });
  });

  describe("#openLogWindow()", function () {
    var mockWindow = {
      document: {
        write: Em.K,
        close: Em.K
      }
    };
    before(function () {
      sinon.stub(window, 'open').returns(mockWindow);
      sinon.spy(mockWindow.document, 'write');
      sinon.spy(mockWindow.document, 'close');
    });
    after(function () {
      window.open.restore();
      mockWindow.document.write.restore();
      mockWindow.document.close.restore();
    });
    it("", function () {
      view.openLogWindow('log');
      expect(window.open.calledOnce).to.be.true;
      expect(mockWindow.document.write.calledWith('log')).to.be.true;
      expect(mockWindow.document.close.calledOnce).to.be.true;
    });
  });
});
