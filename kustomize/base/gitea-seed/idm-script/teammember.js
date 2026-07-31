var _ = require('lib/lodash');

(function () {
  function accessDenied(details) {
    logger.info(details);
    throw {
      code: 403,
      message: 'Access denied'
    };
  }

  function isPatched(field, oldObject) {
    if (!request || !request.patchOperations) {
      return false;
    }
    var operation = request.patchOperations.find(x => x.field == field);
    if (!operation) {
      return false;
    }
    if (!oldObject) {
      return true;
    }

    var property = field.replace(/^\//, '');
    var oldValue = oldObject[property];

    if (property == 'aliasList') {
      var aliasesAdded = _.difference(operation.value, oldValue).length > 0;
      var aliasesRemoved = _.difference(oldValue, operation.value).length > 0;
      return aliasesAdded || aliasesRemoved;
    }

    return operation.value != oldValue;
  }

  function onboardingChecks(object, oldObject) {
    if (!oldObject && !object.password) {
      object.password = require('crypto').generateRandomString([
        { 'rule': 'UPPERCASE', 'minimum': 1 },
        { 'rule': 'LOWERCASE', 'minimum': 1 },
        { 'rule': 'INTEGERS', 'minimum': 1 },
        { 'rule': 'SPECIAL', 'minimum': 1 }
      ], 20);
    }

    var systemIds = ['openidm-admin', 'idm-provisioning', 'org-engine-client'];
    if (systemIds.includes(context.security.authorization.id)) {
      logger.info('Bypassing teammember restrictions for user ' + context.security.authorization.id);
      return;
    }

    if (oldObject) {
      if (oldObject.inviteDate != object.inviteDate) {
        accessDenied('Modification of invite date is not allowed');
      }
      if (oldObject.onboardDate != object.onboardDate) {
        accessDenied('Modification of onboard date is not allowed');
      }
      if (!oldObject.onboardDate && object.accountStatus && object.accountStatus.toLowerCase() == 'active') {
        accessDenied('Cannot set status to active before onboarding');
      }
    } else {
      if (object.accountStatus && object.accountStatus.toLowerCase() == 'active') {
        accessDenied('Creation of active admin is not allowed');
      }
      if (object.onboardDate) {
        accessDenied('Creation of onboarded admin is not allowed');
      }
    }
  }

  function validateGroups(object) {
    if (!object.groups && !object.authzGroups) {
      throw { code: 400, message: 'Either "groups" or "authzGroups" is a required property' };
    }
    if ((object.groups && object.groups.length == 0) && (object.authzGroups && object.authzGroups.length == 0)) {
        throw { code: 400, message: 'Admins must belong to at least one group or one authzGroup' };
    }

    if (object.groups && object.groups.length > 0) {
        const validGroups = [
              'super-admins',
              'tenant-admins'
            ];
        const invalidGroups = object.groups.filter(function (n) { return validGroups.indexOf(n) < 0; });
        if (invalidGroups.length > 0) {
          throw { code: 400, message: 'Invalid groups: ' + invalidGroups.join(', ') };
        }
    }
    if (object.authzGroups && object.authzGroups.length > 0) {
        const validAuthzGroups = [
          "managed/teammembergroup/super-admins",
          "managed/teammembergroup/tenant-admins",
          "managed/teammembergroup/tenant-auditor",
          "managed/teammembergroup/brand-admin",
        ];
        const invalidAuthzGroups = object.authzGroups.filter(function (n) { return validAuthzGroups.indexOf(n._ref) < 0; });
        if (invalidAuthzGroups.length > 0) {
          throw { code: 400, message: 'Invalid authzGroups: ' + invalidAuthzGroups.join(', ') };
        }
    }
  }

  exports.onCreate = function (object) {
    if (!object.groups && !object.authzGroups) {
        object.groups = ['super-admins'];
    }
    validateGroups(object);
    onboardingChecks(object);
  };

  exports.onUpdate = function (object, oldObject) {
    validateGroups(object);
    onboardingChecks(object, oldObject);
  };

  exports.postUpdate = function (object, oldObject) {
    // Email notifications omitted — no SendGrid module in mock tenant.
  };

}());
