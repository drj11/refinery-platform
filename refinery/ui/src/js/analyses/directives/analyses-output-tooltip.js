angular.module('refineryAnalyses')
  .directive("rpAnalysesOutputTooltip", rpAnalysesOutputTooltip);

function rpAnalysesOutputTooltip() {
  "use strict";

  return {
    restrict: 'A',
    link: function(scope, element, attrs)
      {
        $(element)
          .attr('title','Workflow output files are being downloaded from the workflow engine and imported into Refinery as analysis results.')
          .attr('data-container',"body")
          .tooltip({placement: "bottom"});
      }
  };
}
