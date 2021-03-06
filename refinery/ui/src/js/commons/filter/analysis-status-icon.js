angular
  .module('refineryApp')
  .filter('analysisStatusIcon', function () {
    return function(param){
      switch (param) {
        case 'SUCCESS':
          return 'icon-ok-sign';
        case 'FAILURE':
          return 'icon-warning-sign';
        case 'RUNNING':
          return 'icon-cog icon-spin';
        default:
          return 'icon-question-sign';
      }
    };
  });

